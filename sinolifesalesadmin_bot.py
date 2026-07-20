"""
SinolifeSalesAdmin — сотувчилар учун буюртма киритиш боти (тўлиқ версия).

ОҚИМ:
  Сотувчи /start → Bitrix ID юборади → админ тасдиқлайди → доимий боғланади
  Сотувчи [📦 Буюртма киритиш] → сделка ID → Bitrix'дан текширади →
  маҳсулот(лар) танлайди (сон билан) → сумма → регион → манзил → телефон →
  якуний тасдиқ → Bitrix24'га АВТОМАТ ёзилади (Первичный отдел, Успешно) →
  Google Sheets'га ёзилади → сотувчининг РОП гуруҳига эълон қилинади.

ХАР ҚАДАМДА: [⬅️ Орқага] — исталган босқичга қайтиш мумкин.
"""

import os, sys, json, logging, fcntl, re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import urllib.request, urllib.error
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (ApplicationBuilder, CommandHandler, MessageHandler,
                          CallbackQueryHandler, ContextTypes, filters)

_lock = open("/tmp/salesadmin.lock", "w")
try:
    fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.stderr.write("Бот уже запущен.\n"); sys.exit(1)

# ══════════════════════════════ CONFIG ═══════════════════════════════════
TELEGRAM_TOKEN = os.environ.get("SA_TELEGRAM_TOKEN", "")
BITRIX_WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/") + "/"
SHEET_ID = os.environ.get("SA_SHEET_ID", "14-rGmriVBRFUlziFKSKdTVhpl8kK0GOke5Y4OpzYpmM")
SA_JSON = os.environ.get("SA_SA_JSON", "/root/sinolifesalesadmin/service_account.json")
ADMIN_IDS = set()
for x in os.environ.get("SA_ADMIN_IDS", "").split(","):
    x = x.strip()
    if x.isdigit():
        ADMIN_IDS.add(int(x))

TZ = timezone(timedelta(hours=5))
BASE_DIR = Path.home() / "sinolifesalesadmin"
BASE_DIR.mkdir(parents=True, exist_ok=True)
LOCAL_SELLERS = str(BASE_DIR / "sellers.json")
LOCAL_ROP_GROUPS = str(BASE_DIR / "rop_groups.json")
LOCAL_DEPTS_CACHE = str(BASE_DIR / "departments_cache.json")

FIELD_REGION = "UF_CRM_1747975214161"
FIELD_ADDRESS = "UF_CRM_1748964117765"
CATEGORY_PERVICHNY = "12"
WIN_STAGE_ID = "C12:WON"

REGIONS = [
    ("90", "Тошкент ш."), ("92", "Тошкент вил."), ("94", "Андижон"),
    ("96", "Бухоро"), ("98", "Жиззах"), ("100", "Қашқадарё"),
    ("102", "Навоий"), ("104", "Наманган"), ("106", "Самарқанд"),
    ("108", "Сурхондарё"), ("110", "Сирдарё"), ("112", "Фарғона"),
    ("114", "Хоразм"), ("116", "Нукус"),
]
REGION_NAME_BY_ID = {rid: name for rid, name in REGIONS}

CLOTHING_SECTIONS = {"84", "86", "88", "90", "92"}
TOP_PRODUCTS = [
    "Collagen Marine Sinolife", "Kist al hindi Sinolife", "Omega Sinolife",
    "Sedana Sinolife", "Zextra sure", "Zextra maz",
    "Collagen Marmelad Sinolife", "D3+K2 Marmelad Sinolife",
    "Omega Marmelad Sinolife", "Prox",
]
PRODUCTS_PER_PAGE = 8

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

order_state = {}
pending_reqs = {}
_req_counter = [0]

# ══════════════════════════════ Bitrix REST ══════════════════════════════
def _bx(method, params=None):
    url = BITRIX_WEBHOOK + method + ".json"
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def bx_get_deal(deal_id):
    resp = _bx("crm.deal.get", {"id": deal_id})
    if "error" in resp:
        return None, resp.get("error_description", resp["error"])
    return resp.get("result"), None

def bx_get_contact_phone(contact_id):
    if not contact_id:
        return ""
    resp = _bx("crm.contact.get", {"id": contact_id})
    if "error" in resp:
        return ""
    c = resp.get("result") or {}
    phones = c.get("PHONE") or []
    if phones and isinstance(phones, list):
        return phones[0].get("VALUE", "")
    return ""

def bx_get_products():
    products = []
    start = 0
    while True:
        resp = _bx("crm.product.list", {
            "select": ["ID", "NAME", "PRICE", "SECTION_ID", "ACTIVE"],
            "order": {"NAME": "ASC"},
            "start": start,
        })
        if "error" in resp:
            log.error("bx_get_products: %s", resp); break
        batch = resp.get("result", [])
        for p in batch:
            section = str(p.get("SECTION_ID") or "")
            if section in CLOTHING_SECTIONS:
                continue
            if p.get("ACTIVE") != "Y":
                continue
            products.append(p)
        nxt = resp.get("next")
        if not nxt:
            break
        start = nxt
    return _sort_products(products)

def _sort_products(products):
    top = []
    for name in TOP_PRODUCTS:
        for p in products:
            if p["NAME"] == name:
                top.append(p); break
    top_ids = {p["ID"] for p in top}
    rest = [p for p in products if p["ID"] not in top_ids]
    return top + rest

def bx_set_products(deal_id, rows):
    return _bx("crm.deal.productrows.set", {"id": deal_id, "rows": rows})

def bx_update_deal(deal_id, fields):
    return _bx("crm.deal.update", {"id": deal_id, "fields": fields})

def bx_get_user(bitrix_id):
    resp = _bx("user.get", {"ID": bitrix_id})
    if "error" in resp:
        return None
    res = resp.get("result") or []
    return res[0] if res else None

EMPLOYEE_EMAIL_DOMAIN = os.environ.get("SA_EMPLOYEE_EMAIL_DOMAIN", "sinolifemanager.uz")

def bx_find_user_by_number(number):
    """Ходим рақами (исм-фамилиягa қўшилган, email'га мос) орқали қидиради.
    Мисол: 249 -> email 249@sinolifemanager.uz."""
    email = number + "@" + EMPLOYEE_EMAIL_DOMAIN
    resp = _bx("user.get", {"filter": {"EMAIL": email}})
    if "error" in resp:
        return None
    res = resp.get("result") or []
    return res[0] if res else None

def bx_get_departments():
    if os.path.exists(LOCAL_DEPTS_CACHE):
        with open(LOCAL_DEPTS_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
        if cached.get("ts", 0) > datetime.now(TZ).timestamp() - 6 * 3600:
            return cached["depts"]
    resp = _bx("department.get", {})
    depts = resp.get("result", []) if "error" not in resp else []
    with open(LOCAL_DEPTS_CACHE, "w", encoding="utf-8") as f:
        json.dump({"ts": datetime.now(TZ).timestamp(), "depts": depts}, f, ensure_ascii=False)
    return depts

def find_rop_for_department(dept_id, depts):
    by_id = {str(d["ID"]): d for d in depts}
    cur = str(dept_id)
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        d = by_id.get(cur)
        if not d:
            return None
        if "(ROP)" in (d.get("NAME") or ""):
            return {"dept_id": d["ID"], "name": d["NAME"], "head_bitrix_id": d.get("UF_HEAD")}
        cur = str(d.get("PARENT") or "")
    return None

def resolve_seller_rop(bitrix_user):
    depts = bx_get_departments()
    for dept_id in (bitrix_user.get("UF_DEPARTMENT") or []):
        rop = find_rop_for_department(dept_id, depts)
        if rop:
            return rop
    return None

# ══════════════════════════════ Local storage ════════════════════════════
def _load(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default

def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_sellers(): return _load(LOCAL_SELLERS, {})
def save_sellers(d): _save(LOCAL_SELLERS, d)
def load_rop_groups(): return _load(LOCAL_ROP_GROUPS, {})
def save_rop_groups(d): _save(LOCAL_ROP_GROUPS, d)

def is_admin(uid):
    return uid in ADMIN_IDS

def get_seller_by_tg(tg_user_id):
    sellers = load_sellers()
    for bid, info in sellers.items():
        if info.get("tg_user_id") == tg_user_id:
            return bid, info
    return None, None

# ══════════════════════════════ Google Sheets ════════════════════════════
SHEETS_HEADERS = ["Sana", "Vaqt", "Sotuvchi", "Bitrix_ID", "Sdelka_ID",
                  "Mahsulotlar", "Summa", "Region", "Manzil", "Telefon", "ROP"]

def _book():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(SA_JSON, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID)

def ensure_sheet():
    book = _book()
    try:
        ws = book.worksheet("Buyurtmalar")
    except Exception:
        ws = book.add_worksheet(title="Buyurtmalar", rows=5000, cols=len(SHEETS_HEADERS) + 2)
        ws.append_row(SHEETS_HEADERS)
    return ws

def sheets_log_order(seller_name, bitrix_id, deal_id, products_str, summa, region_name,
                     address, phone, rop_name):
    try:
        ws = ensure_sheet()
        now = datetime.now(TZ)
        ws.append_row([now.strftime("%d.%m.%Y"), now.strftime("%H:%M"), seller_name,
                       bitrix_id, deal_id, products_str, summa, region_name, address,
                       phone, rop_name or ""])
    except Exception as e:
        log.error("sheets_log_order: %s", e)

# ══════════════════════════════ /start va boglash ═════════════════════════
async def cmd_start(update, context):
    u = update.effective_user
    bid, seller = get_seller_by_tg(u.id)
    if seller:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 Буюртма киритиш", callback_data="neworder")]])
        await update.message.reply_text(
            "👋 Салом, " + seller["name"] + "!\n\nБуюртма киритиш учун тугмани босинг:",
            reply_markup=kb)
        return
    await update.message.reply_text(
        "👋 Салом! Сизни тизимга боғлаш керак.\n\n"
        "Ходим рақамингизни юборинг (исм-фамилиянгизга қўшилган рақам, "
        "масалан: 249).\nБилмасангиз — админдан сўранг.")
    order_state[u.id] = {"step": "link_wait_id"}

async def handle_link_id(update, context, uid, text):
    if not text.isdigit():
        await update.message.reply_text("❌ Фақат рақам юборинг (масалан: 249)."); return
    bitrix_user = bx_find_user_by_number(text)
    if not bitrix_user:
        await update.message.reply_text(
            "❌ Бундай рақамли ходим топилмади (" + text + "@" + EMPLOYEE_EMAIL_DOMAIN +
            "). Қайта текшириб ёзинг."); return
    real_bitrix_id = bitrix_user["ID"]  # Bitrix ички ID — сақлаш учун
    full_name = (bitrix_user.get("NAME", "") + " " + bitrix_user.get("LAST_NAME", "")).strip()
    _req_counter[0] += 1
    req_id = str(_req_counter[0])
    u = update.effective_user
    pending_reqs[req_id] = {
        "tg_user_id": uid, "tg_username": u.username or "", "tg_name": u.full_name,
        "bitrix_id": real_bitrix_id, "employee_number": text, "bitrix_name": full_name,
    }
    order_state.pop(uid, None)
    await update.message.reply_text(
        "⏳ Сўровингиз админга юборилди: " + full_name + " (ID " + text + ")\n"
        "Тасдиқлангач хабар келади.")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Тасдиқлаш", callback_data="linkok:" + req_id),
        InlineKeyboardButton("❌ Рад этиш", callback_data="linkno:" + req_id)]])
    text_admin = ("🔗 Янги боғлаш сўрови:\n"
                 "Telegram: " + u.full_name + " (@" + (u.username or "-") + ")\n"
                 "Bitrix: " + full_name + " (ходим рақами: " + text +
                 ", Bitrix ID: " + str(real_bitrix_id) + ")")
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=text_admin, reply_markup=kb)
        except Exception as e:
            log.error("admin notify: %s", e)

async def cb_link_approve(update, context):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    req_id = q.data.split("linkok:", 1)[1]
    req = pending_reqs.pop(req_id, None)
    if not req:
        await q.edit_message_text("⚠️ Сўров топилмади (эскирган бўлиши мумкин)."); return
    bitrix_user = bx_get_user(req["bitrix_id"])
    rop = resolve_seller_rop(bitrix_user) if bitrix_user else None
    sellers = load_sellers()
    sellers[req["bitrix_id"]] = {
        "name": req["bitrix_name"], "tg_user_id": req["tg_user_id"],
        "tg_username": req["tg_username"], "employee_number": req.get("employee_number", ""),
        "rop_bitrix_id": rop["head_bitrix_id"] if rop else None,
        "rop_name": rop["name"] if rop else None,
    }
    save_sellers(sellers)
    await q.edit_message_text("✅ Тасдиқланди: " + req["bitrix_name"])
    try:
        await context.bot.send_message(chat_id=req["tg_user_id"],
            text="✅ Сиз тасдиқландингиз! Энди /start билан буюртма кирита оласиз.")
    except Exception as e:
        log.error("notify seller: %s", e)

async def cb_link_reject(update, context):
    q = update.callback_query; await q.answer()
    if q.from_user.id not in ADMIN_IDS:
        return
    req_id = q.data.split("linkno:", 1)[1]
    req = pending_reqs.pop(req_id, None)
    if not req:
        await q.edit_message_text("⚠️ Сўров топилмади."); return
    await q.edit_message_text("❌ Рад этилди: " + req["bitrix_name"])
    try:
        await context.bot.send_message(chat_id=req["tg_user_id"],
            text="❌ Сўровингиз рад этилди. Админга мурожаат қилинг.")
    except Exception:
        pass

# ══════════════════════════════ РОП гуруҳлари (админ) ═════════════════════
async def cmd_addropgroup(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Фақат админ."); return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Қўллаш: /addropgroup РОП_Bitrix_ID ГУРУҲ_ID\n"
            "(РОП Bitrix ID — /listrops билан кўринг)"); return
    rop_id, chat_id = args[0], args[1]
    groups = load_rop_groups()
    groups[rop_id] = chat_id
    save_rop_groups(groups)
    await update.message.reply_text("✅ РОП гуруҳи сақланди: " + rop_id + " -> " + chat_id)

async def cmd_listrops(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Фақат админ."); return
    depts = bx_get_departments()
    rops = [d for d in depts if "(ROP)" in (d.get("NAME") or "")]
    if not rops:
        await update.message.reply_text("РОП топилмади."); return
    lines = ["🏢 РОПлар рўйхати:", ""]
    for d in rops:
        lines.append(d["NAME"] + " — Bitrix ID: " + str(d.get("UF_HEAD", "?")) +
                     " (бўлим " + str(d["ID"]) + ")")
    await update.message.reply_text("\n".join(lines))

async def cmd_whoami(update, context):
    u = update.effective_user
    bid, seller = get_seller_by_tg(u.id)
    txt = "🆔 Telegram ID: " + str(u.id) + " · @" + (u.username or "йўқ")
    if seller:
        txt += "\n🔗 Bitrix ID: " + bid + " (" + seller["name"] + ")"
        txt += "\n🏢 РОП: " + (seller.get("rop_name") or "белгиланмаган")
    await update.message.reply_text(txt)

# ══════════════════════════════ Буюртма — бошланиш ═══════════════════════
STEP_ORDER = ["deal_id", "products", "qty", "amount", "region", "address", "phone", "confirm"]

async def cb_neworder(update, context):
    q = update.callback_query; await q.answer()
    u = q.from_user
    bid, seller = get_seller_by_tg(u.id)
    if not seller:
        await q.edit_message_text("⛔ Сиз рўйхатда йўқсиз. /start билан боғланинг."); return
    order_state[u.id] = {"step": "deal_id", "data": {}, "history": [],
                         "bitrix_id": bid, "seller": seller}
    await q.edit_message_text("📦 Сделка ID'сини юборинг (сделкани топишимиз учун):")

async def handle_deal_id(update, context, uid, text):
    st = order_state[uid]
    if not text.isdigit():
        await update.message.reply_text("❌ Фақат рақам (сделка ID) юборинг."); return
    deal, err = bx_get_deal(text)
    if err or not deal:
        await update.message.reply_text("❌ Сделка топилмади. Қайта текшириб ёзинг: " + str(err or "")); return
    if str(deal.get("CATEGORY_ID")) != CATEGORY_PERVICHNY:
        await update.message.reply_text(
            "⚠️ Бу сделка «Первичный отдел» воронкасида эмас. Давом этиб бўлмайди."); return
    st["deal_id"] = text
    st["deal"] = deal
    await update.message.reply_text(
        "✅ Сделка топилди: " + (deal.get("TITLE") or "№" + text) + "\n\nДавом этамиз...")
    await ask_products(update.message, uid)

async def ask_products(msg, uid):
    st = order_state[uid]
    st["products"] = bx_get_products()
    st["selected"] = {}
    st["page"] = 0
    st["step"] = "products"
    push_history(uid, "deal_id")
    await msg.reply_text(
        "2️⃣ Маҳсулот(лар)ни танланг (бир нечтасини танлаш мумкин):",
        reply_markup=build_product_kb(st))

def push_history(uid, step_name):
    order_state[uid]["history"].append(step_name)

def build_product_kb(st):
    products = st["products"]; page = st["page"]; selected = st["selected"]
    total_pages = max(1, (len(products) + PRODUCTS_PER_PAGE - 1) // PRODUCTS_PER_PAGE)
    start = page * PRODUCTS_PER_PAGE
    chunk = products[start:start + PRODUCTS_PER_PAGE]
    rows = []
    for p in chunk:
        pid = p["ID"]
        mark = "✅ " if pid in selected else ""
        rows.append([InlineKeyboardButton(mark + p["NAME"], callback_data="prod:" + pid)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data="prodpage:" + str(page - 1)))
    nav.append(InlineKeyboardButton(str(page + 1) + "/" + str(total_pages), callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data="prodpage:" + str(page + 1)))
    rows.append(nav)
    done_label = "✅ Тайёр (" + str(len(selected)) + " танланди)" if selected else "✅ Тайёр"
    rows.append([InlineKeyboardButton(done_label, callback_data="proddone")])
    rows.append([InlineKeyboardButton("⬅️ Орқага", callback_data="back")])
    return InlineKeyboardMarkup(rows)

async def cb_prod_toggle(update, context):
    q = update.callback_query; await q.answer()
    st = order_state.get(q.from_user.id)
    if not st or st.get("step") != "products":
        return
    pid = q.data.split("prod:", 1)[1]
    if pid in st["selected"]:
        del st["selected"][pid]
    else:
        prod = next((p for p in st["products"] if p["ID"] == pid), None)
        if prod:
            st["selected"][pid] = {"name": prod["NAME"], "price": float(prod.get("PRICE") or 0), "qty": None}
    await q.edit_message_reply_markup(reply_markup=build_product_kb(st))

async def cb_prod_page(update, context):
    q = update.callback_query; await q.answer()
    st = order_state.get(q.from_user.id)
    if not st or st.get("step") != "products":
        return
    st["page"] = int(q.data.split("prodpage:", 1)[1])
    await q.edit_message_reply_markup(reply_markup=build_product_kb(st))

async def cb_noop(update, context):
    await update.callback_query.answer()

async def cb_prod_done(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    st = order_state.get(uid)
    if not st or st.get("step") != "products":
        return
    if not st["selected"]:
        await q.answer("Камида битта маҳсулот танланг!", show_alert=True); return
    st["qty_queue"] = list(st["selected"].keys())
    st["step"] = "qty"
    push_history(uid, "products")
    pid = st["qty_queue"][0]
    name = st["selected"][pid]["name"]
    await q.edit_message_text("🔢 «" + name + "» — нечта дона? (рақам ёзинг):")

async def handle_qty(update, context, uid, text):
    st = order_state[uid]
    if not text.isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Мусбат бутун рақам ёзинг (масалан: 2)."); return
    pid = st["qty_queue"].pop(0)
    st["selected"][pid]["qty"] = int(text)
    if st["qty_queue"]:
        next_pid = st["qty_queue"][0]
        name = st["selected"][next_pid]["name"]
        await update.message.reply_text("🔢 «" + name + "» — нечта дона? (рақам ёзинг):")
    else:
        await ask_amount(update.message, uid)

async def ask_amount(msg, uid):
    st = order_state[uid]
    total = sum(p["price"] * p["qty"] for p in st["selected"].values())
    st["computed_amount"] = total
    st["step"] = "amount"
    push_history(uid, "qty")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ " + fmt_money(total) + " сум", callback_data="amtdef")],
        [InlineKeyboardButton("✏️ Бошқа сумма", callback_data="amtcustom")],
        [InlineKeyboardButton("⬅️ Орқага", callback_data="back")]])
    await msg.reply_text("3️⃣ Сумма:", reply_markup=kb)

def fmt_money(n):
    return f"{n:,.0f}".replace(",", " ")

def parse_money(text):
    """'350 000', '350,000', '350.000' -> 350000.0 (бўшлиқ/вергул/нуқтани тозалайди)."""
    s = re.sub(r"[ \u00a0.,]", "", text.strip())
    if not s.isdigit():
        return None
    return float(s)

async def cb_amount_default(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    st = order_state.get(uid)
    if not st or st.get("step") != "amount":
        return
    st["amount"] = st["computed_amount"]
    await ask_region(q, uid, edit=True)

async def cb_amount_custom(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    st = order_state.get(uid)
    if not st or st.get("step") != "amount":
        return
    st["step"] = "amount_custom"
    await q.edit_message_text("✏️ Суммани ёзинг (мисол: 350000 ёки 350 000):")

async def handle_amount_custom(update, context, uid, text):
    st = order_state[uid]
    amt = parse_money(text)
    if amt is None or amt <= 0:
        await update.message.reply_text("❌ Тўғри сумма киритинг (мисол: 350000)."); return
    st["amount"] = amt
    st["step"] = "amount"
    await ask_region(update.message, uid)

async def ask_region(msg_or_q, uid, edit=False):
    st = order_state[uid]
    st["step"] = "region"
    push_history(uid, "amount")
    buttons = [InlineKeyboardButton(name, callback_data="region:" + rid) for rid, name in REGIONS]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅️ Орқага", callback_data="back")])
    kb = InlineKeyboardMarkup(rows)
    text = "4️⃣ Регионни танланг:"
    if edit:
        await msg_or_q.edit_message_text(text, reply_markup=kb)
    else:
        await msg_or_q.reply_text(text, reply_markup=kb)

async def cb_region(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    st = order_state.get(uid)
    if not st or st.get("step") != "region":
        return
    rid = q.data.split("region:", 1)[1]
    st["region_id"] = rid
    st["region_name"] = REGION_NAME_BY_ID.get(rid, "")
    st["step"] = "address"
    push_history(uid, "region")
    await q.edit_message_text("5️⃣ Манзилни ёзинг (эркин матн):")

async def handle_address(update, context, uid, text):
    st = order_state[uid]
    if not text.strip():
        await update.message.reply_text("❌ Манзил бўш бўлмасин."); return
    st["address"] = text.strip()
    await ask_phone(update.message, uid)

async def ask_phone(msg, uid):
    st = order_state[uid]
    deal = st["deal"]
    contact_id = deal.get("CONTACT_ID")
    phone = bx_get_contact_phone(contact_id)
    st["bitrix_phone"] = phone
    st["step"] = "phone"
    push_history(uid, "address")
    rows = []
    if phone:
        rows.append([InlineKeyboardButton("✅ " + phone, callback_data="phonedef")])
    rows.append([InlineKeyboardButton("✏️ Бошқа/қўшимча рақам", callback_data="phonecustom")])
    rows.append([InlineKeyboardButton("⬅️ Орқага", callback_data="back")])
    await msg.reply_text("6️⃣ Телефон:", reply_markup=InlineKeyboardMarkup(rows))

async def cb_phone_default(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    st = order_state.get(uid)
    if not st or st.get("step") != "phone":
        return
    st["phone"] = st["bitrix_phone"]
    await show_confirm(q, uid, edit=True)

async def cb_phone_custom(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    st = order_state.get(uid)
    if not st or st.get("step") != "phone":
        return
    st["step"] = "phone_custom"
    await q.edit_message_text("✏️ Телефон рақамини ёзинг (+998XXXXXXXXX кўринишида):")

PHONE_RE = re.compile(r"^\+998\d{9}$")

async def handle_phone_custom(update, context, uid, text):
    st = order_state[uid]
    cleaned = text.strip().replace(" ", "").replace("-", "")
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    if not PHONE_RE.match(cleaned):
        await update.message.reply_text("❌ Формат нотўғри. +998XXXXXXXXX кўринишида ёзинг."); return
    st["phone"] = cleaned
    st["step"] = "phone"
    await show_confirm(update.message, uid)

# ══════════════════════════════ Тасдиқ ва ёзиш ═══════════════════════════
async def show_confirm(msg_or_q, uid, edit=False):
    st = order_state[uid]
    st["step"] = "confirm"
    push_history(uid, "phone")
    lines = ["📋 Тасдиқланг:", "",
             "🆔 Сделка: #" + st["deal_id"]]
    for p in st["selected"].values():
        lines.append("📦 " + p["name"] + " × " + str(p["qty"]))
    lines.append("💰 Сумма: " + fmt_money(st["amount"]) + " сум")
    lines.append("📍 Регион: " + st["region_name"])
    lines.append("🏠 Манзил: " + st["address"])
    lines.append("📞 Телефон: " + st["phone"])
    text = "\n".join(lines)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Тасдиқлаш", callback_data="confirm")],
        [InlineKeyboardButton("⬅️ Орқага", callback_data="back")]])
    if edit:
        await msg_or_q.edit_message_text(text, reply_markup=kb)
    else:
        await msg_or_q.reply_text(text, reply_markup=kb)

async def cb_confirm(update, context):
    q = update.callback_query; await q.answer("Юборилмоқда...")
    uid = q.from_user.id
    st = order_state.get(uid)
    if not st or st.get("step") != "confirm":
        return
    deal_id = st["deal_id"]

    # 1) Bitrix'га маҳсулотларни ёзамиз
    rows = [{"PRODUCT_ID": pid, "PRICE": p["price"], "QUANTITY": p["qty"]}
            for pid, p in st["selected"].items()]
    resp1 = bx_set_products(deal_id, rows)
    if "error" in resp1:
        await q.edit_message_text("❌ Bitrix хато (маҳсулот): " + str(resp1)); return

    # 2) Сделкани янгилаймиз (сумма, регион, манзил, статус)
    fields = {
        "OPPORTUNITY": st["amount"],
        FIELD_REGION: st["region_id"],
        FIELD_ADDRESS: st["address"],
        "STAGE_ID": WIN_STAGE_ID,
    }
    resp2 = bx_update_deal(deal_id, fields)
    if "error" in resp2:
        await q.edit_message_text("❌ Bitrix хато (янгилаш): " + str(resp2)); return

    seller = st["seller"]
    products_str = ", ".join(p["name"] + " x" + str(p["qty"]) for p in st["selected"].values())

    # 3) Google Sheets'га ёзамиз
    sheets_log_order(seller["name"], st["bitrix_id"], deal_id, products_str,
                     st["amount"], st["region_name"], st["address"], st["phone"],
                     seller.get("rop_name"))

    # 4) РОП гуруҳига эълон
    announce = ("✅ ЯНГИ БУЮРТМА\n\n"
               "👤 Сотувчи: " + seller["name"] + "\n"
               "🆔 Сделка: #" + deal_id + "\n"
               "📦 " + products_str + "\n"
               "💰 " + fmt_money(st["amount"]) + " сум\n"
               "📍 " + st["region_name"] + ", " + st["address"] + "\n"
               "📞 " + st["phone"])
    rop_groups = load_rop_groups()
    target_chat = rop_groups.get(str(seller.get("rop_bitrix_id"))) if seller.get("rop_bitrix_id") else None
    sent = False
    if target_chat:
        try:
            await context.bot.send_message(chat_id=target_chat, text=announce)
            sent = True
        except Exception as e:
            log.error("rop announce: %s", e)
    if not sent:
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=aid, text=announce + "\n\n⚠️ РОП гуруҳи топилмади.")
            except Exception:
                pass

    await q.edit_message_text("✅ Буюртма муваффақиятли сақланди ва Bitrix24'га ёзилди!")
    order_state.pop(uid, None)

# ══════════════════════════════ Орқага (⬅️) ═══════════════════════════════
async def cb_back(update, context):
    q = update.callback_query; await q.answer()
    uid = q.from_user.id
    st = order_state.get(uid)
    if not st or not st.get("history"):
        await q.edit_message_text("⬅️ Бошидан бошланг: /start"); order_state.pop(uid, None); return
    prev_step = st["history"].pop()
    if prev_step == "deal_id":
        st["step"] = "deal_id"
        await q.edit_message_text("📦 Сделка ID'сини юборинг:")
    elif prev_step == "products":
        st["step"] = "products"
        await q.edit_message_text("2️⃣ Маҳсулот(лар)ни танланг:", reply_markup=build_product_kb(st))
    elif prev_step == "qty":
        # маҳсулот танлашга қайтамиз (сон бекор қилинади)
        st["selected"] = {k: {**v, "qty": None} for k, v in st["selected"].items()}
        st["step"] = "products"
        await q.edit_message_text("2️⃣ Маҳсулот(лар)ни танланг:", reply_markup=build_product_kb(st))
    elif prev_step == "amount":
        await ask_amount(q.message, uid)
        st["history"].pop()  # ask_amount ўзи push қилади, дублика олдини оламиз
    elif prev_step == "region":
        await ask_region(q, uid, edit=True)
        st["history"].pop()
    elif prev_step == "address":
        st["step"] = "address"
        await q.edit_message_text("5️⃣ Манзилни ёзинг (эркин матн):")
    elif prev_step == "phone":
        await ask_phone(q.message, uid)
        st["history"].pop()
    else:
        await q.edit_message_text("⬅️ Бошидан бошланг: /start"); order_state.pop(uid, None)

# ══════════════════════════════ Матн диспетчери ═══════════════════════════
async def on_text(update, context):
    u = update.effective_user
    msg = update.message
    if not msg or not msg.text:
        return
    uid = u.id
    st = order_state.get(uid)
    if not st:
        return
    step = st.get("step")
    text = msg.text.strip()
    if step == "link_wait_id":
        await handle_link_id(update, context, uid, text)
    elif step == "deal_id":
        await handle_deal_id(update, context, uid, text)
    elif step == "qty":
        await handle_qty(update, context, uid, text)
    elif step == "amount_custom":
        await handle_amount_custom(update, context, uid, text)
    elif step == "address":
        await handle_address(update, context, uid, text)
    elif step == "phone_custom":
        await handle_phone_custom(update, context, uid, text)

# ══════════════════════════════ Запуск ═══════════════════════════════════
if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        sys.exit("❌ SA_TELEGRAM_TOKEN o'rnatilmagan")
    if not BITRIX_WEBHOOK or BITRIX_WEBHOOK == "/":
        sys.exit("❌ BITRIX_WEBHOOK o'rnatilmagan")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("addropgroup", cmd_addropgroup))
    app.add_handler(CommandHandler("listrops", cmd_listrops))

    app.add_handler(CallbackQueryHandler(cb_link_approve, pattern=r"^linkok:"))
    app.add_handler(CallbackQueryHandler(cb_link_reject, pattern=r"^linkno:"))
    app.add_handler(CallbackQueryHandler(cb_neworder, pattern=r"^neworder$"))
    app.add_handler(CallbackQueryHandler(cb_prod_toggle, pattern=r"^prod:"))
    app.add_handler(CallbackQueryHandler(cb_prod_page, pattern=r"^prodpage:"))
    app.add_handler(CallbackQueryHandler(cb_prod_done, pattern=r"^proddone$"))
    app.add_handler(CallbackQueryHandler(cb_noop, pattern=r"^noop$"))
    app.add_handler(CallbackQueryHandler(cb_amount_default, pattern=r"^amtdef$"))
    app.add_handler(CallbackQueryHandler(cb_amount_custom, pattern=r"^amtcustom$"))
    app.add_handler(CallbackQueryHandler(cb_region, pattern=r"^region:"))
    app.add_handler(CallbackQueryHandler(cb_phone_default, pattern=r"^phonedef$"))
    app.add_handler(CallbackQueryHandler(cb_phone_custom, pattern=r"^phonecustom$"))
    app.add_handler(CallbackQueryHandler(cb_confirm, pattern=r"^confirm$"))
    app.add_handler(CallbackQueryHandler(cb_back, pattern=r"^back$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("SinolifeSalesAdmin bot ishga tushdi.")
    app.run_polling()