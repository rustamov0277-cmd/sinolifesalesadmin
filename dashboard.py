# -*- coding: utf-8 -*-
"""
SinolifeSalesAdmin — РОП дашбоардлари генератори.

deal_state.json'дан ўқиб, ҳар РОП учун алоҳида HTML саҳифа яратади
(жами сана бўйича филтр, статус бўйича филтр, қидирув билан).
Cron орқали ҳар N дақиқада ишга туширилади ва GitHub'га push қилинади.

Ишлатиш:  python3 dashboard.py
Натижа:   docs/index.html  ва  docs/<rop-slug>-<hash>.html
"""
import json
import hashlib
import html
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ = timezone(timedelta(hours=5))  # Тошкент

STATE_DIR = Path(os.environ.get("SA_STATE_DIR", "/root/sinolifesalesadmin_v2/state"))
OUT_DIR = Path(os.environ.get("SA_DASHBOARD_DIR", "/root/sales_dashboard/docs"))
# Ҳавола тахмин қилинмаслиги учун — ҳар РОП файл номига қўшиладиган махфий сўз.
# start.sh'да SA_DASHBOARD_SALT="uzun-tasodifiy-sirli-soz" қилиб қўйинг.
SALT = os.environ.get("SA_DASHBOARD_SALT", "sinolife-default-salt")

STATUS_ORDER = ["confirm_new", "no_answer", "confirmed", "rejected"]
STATUS_LABELS = {
    "confirm_new": ("🕔", "Тасдиқлаш"),
    "no_answer":   ("🟡", "Кутармади (нд)"),
    "confirmed":   ("✅", "Тасдиқланди"),
    "rejected":    ("❌", "Тасдиқланмади"),
}


def slugify(name):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "nomalum")).strip("-").lower()
    return s or "nomalum"


def rop_filename(rop_name):
    """Тахмин қилиб бўлмайдиган файл номи: nom-<12 belgili hash>.html"""
    digest = hashlib.sha256((SALT + "|" + (rop_name or "")).encode()).hexdigest()[:12]
    return f"{slugify(rop_name)}-{digest}.html"


def parse_last_text(text):
    """Бот юборган хабар матнидан майдонларни ажратади (формат ўзимизники,
    шунинг учун ишончли)."""
    out = {"order_num": "", "deal_id": "", "products": [], "summa": "",
           "region": "", "address": "", "client": "", "phones": [],
           "operator": "", "source": ""}
    if not text:
        return out
    lines = text.split("\n")
    in_products = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("№"):
            out["order_num"] = stripped[1:].strip()
            in_products = False
        elif "Id сделки:" in stripped:
            out["deal_id"] = stripped.split(":", 1)[1].strip()
            in_products = False
        elif stripped.startswith("📦"):
            out["products"].append(stripped.split(":", 1)[1].strip())
            in_products = True
        elif stripped.startswith("💵"):
            out["summa"] = stripped.split(":", 1)[1].strip()
            in_products = False
        elif stripped.startswith("📍"):
            out["region"] = stripped.split(":", 1)[1].strip()
            in_products = False
        elif stripped.startswith("🚚"):
            out["address"] = stripped.split(":", 1)[1].strip()
            in_products = False
        elif stripped.startswith("👤"):
            out["client"] = stripped.split(":", 1)[1].strip()
            in_products = False
        elif stripped.startswith("📞"):
            out["phones"].append(stripped.split(":", 1)[1].strip())
            in_products = False
        elif stripped.startswith("Оператор:"):
            out["operator"] = stripped.split(":", 1)[1].strip()
            in_products = False
        elif stripped.startswith("🌐"):
            out["source"] = stripped.split(":", 1)[1].strip()
            in_products = False
        elif in_products and stripped:
            out["products"].append(stripped)
    return out


def load_orders():
    """deal_state.json'дан барча буюртмаларни ўқиб, РОП бўйича гуруҳлайди."""
    path = STATE_DIR / "deal_state.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    by_rop = {}
    for deal_id, entry in data.items():
        rop = entry.get("rop_name") or "Номаълум"
        parsed = parse_last_text(entry.get("last_text", ""))
        created = entry.get("created_at") or entry.get("sent_at") or ""
        try:
            dt = datetime.fromisoformat(created).astimezone(TZ)
            date_str = dt.strftime("%Y-%m-%d")
            time_str = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            date_str, time_str = "", ""

        by_rop.setdefault(rop, []).append({
            "deal_id": deal_id,
            "order_num": parsed["order_num"],
            "date": date_str,
            "time": time_str,
            "products": parsed["products"],
            "summa": parsed["summa"],
            "region": parsed["region"],
            "address": parsed["address"],
            "client": parsed["client"],
            "phones": parsed["phones"],
            "operator": parsed["operator"],
            "source": parsed["source"],
            "status": entry.get("status_key", "confirm_new"),
        })

    for rop in by_rop:
        by_rop[rop].sort(key=lambda x: (x["date"], x["time"]), reverse=True)
    return by_rop


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f1419;--panel:#171d26;--panel2:#1c2430;--border:#2a3441;
  --text:#e8edf3;--muted:#8b98a8;--accent:#4f8bf0;--accent2:#7c5cff;
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:linear-gradient(160deg,#0f1419 0%,#141a22 100%);color:var(--text);
padding:18px;font-size:14px;min-height:100vh}
h1{font-size:22px;margin-bottom:4px;font-weight:700;
background:linear-gradient(90deg,#7c5cff,#4f8bf0);-webkit-background-clip:text;
background-clip:text;-webkit-text-fill-color:transparent;display:inline-block}
.sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.stat{background:var(--panel);border:1px solid var(--border);border-radius:12px;
padding:12px 16px;min-width:110px;transition:transform .15s,border-color .15s}
.stat:hover{transform:translateY(-2px);border-color:var(--accent)}
.stat .n{font-size:24px;font-weight:800;color:var(--text)}
.stat .l{font-size:11px;color:var(--muted);margin-top:3px;text-transform:uppercase;
letter-spacing:.03em}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center}
input,select,button{padding:9px 12px;border:1px solid var(--border);border-radius:8px;
font-size:14px;background:var(--panel);color:var(--text);font-family:inherit}
input::placeholder{color:var(--muted)}
input[type=text]{min-width:220px;flex:1}
input[type=date]{color-scheme:dark}
.btn{cursor:pointer;font-weight:600;transition:background .15s,border-color .15s}
.btn:hover{border-color:var(--accent);background:#212b38}
.btn-primary{background:linear-gradient(90deg,var(--accent2),var(--accent));border:none;
color:#fff}
.btn-primary:hover{filter:brightness(1.1)}
.table-wrap{overflow-x:auto;border-radius:12px;border:1px solid var(--border);
background:var(--panel)}
table{width:100%;border-collapse:collapse;min-width:1080px}
th{background:var(--panel2);padding:11px 12px;text-align:left;font-size:11px;font-weight:700;
color:var(--muted);white-space:nowrap;text-transform:uppercase;letter-spacing:.04em;
border-bottom:1px solid var(--border);position:sticky;top:0}
td{padding:10px 12px;border-top:1px solid var(--border);vertical-align:top;white-space:nowrap}
td.wrap{white-space:normal}
tr{transition:background .1s}
tr:hover td{background:#1e2733}
.badge{display:inline-block;padding:4px 10px;border-radius:20px;font-size:12px;
white-space:nowrap;font-weight:600;border:1px solid transparent}
.s-confirm_new{background:#3a2f0d;color:#ffcc4d;border-color:#5a4a15}
.s-no_answer{background:#3a2a0d;color:#ffb84d;border-color:#5a4015}
.s-confirmed{background:#0d3a20;color:#4dffa0;border-color:#155a35}
.s-rejected{background:#3a0d14;color:#ff6b7a;border-color:#5a1520}
.prod{font-size:13px;line-height:1.5;color:var(--text)}
.muted{color:var(--muted);font-size:12px}
.nowrap{white-space:nowrap}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.rop-list{display:grid;gap:12px;grid-template-columns:repeat(auto-fill,minmax(250px,1fr))}
.rop-card{background:var(--panel);border:1px solid var(--border);border-radius:12px;
padding:16px;transition:transform .15s,border-color .15s}
.rop-card:hover{transform:translateY(-3px);border-color:var(--accent)}
.rop-card .name{font-weight:700;font-size:17px;margin-bottom:8px}
.rop-card .name a{color:var(--text)}
.ph{display:inline-flex;align-items:center;gap:6px}
.eye{border:1px solid var(--border);background:var(--panel2);border-radius:6px;cursor:pointer;
padding:3px 7px;font-size:12px;line-height:1.4;color:var(--muted)}
.eye:hover{color:var(--text);border-color:var(--accent)}
@media(max-width:700px){
 body{padding:10px}
 .stat{min-width:90px;padding:10px 12px}
 .stat .n{font-size:20px}
 input[type=text]{min-width:150px}
}
"""

def build_js(today_str):
    return f"""
var TODAY = "{today_str}";
function togglePhone(btn){{
  var wrap=btn.closest('.ph');
  var span=wrap.querySelector('.pm');
  var full=wrap.dataset.full;
  if(btn.dataset.shown==='1'){{
    span.textContent=full.slice(0,6)+'***'+full.slice(-4);
    btn.dataset.shown='0'; btn.textContent='👁'; btn.title='Кўрсатиш';
  }}else{{
    span.textContent=full;
    btn.dataset.shown='1'; btn.textContent='🙈'; btn.title='Яшириш';
  }}
}}
function applyFilters(){{
  var q=document.getElementById('q').value.toLowerCase();
  var st=document.getElementById('st').value;
  var d1=document.getElementById('d1').value;
  var d2=document.getElementById('d2').value;
  var rows=document.querySelectorAll('tbody tr');
  var shown=0;
  rows.forEach(function(r){{
    var ok=true;
    if(st && r.dataset.status!==st) ok=false;
    if(ok && d1 && r.dataset.date < d1) ok=false;
    if(ok && d2 && r.dataset.date > d2) ok=false;
    if(ok && q){{
      var hay=(r.innerText+' '+(r.dataset.phones||'')).toLowerCase();
      if(hay.indexOf(q)===-1) ok=false;
    }}
    r.style.display = ok ? '' : 'none';
    if(ok) shown++;
  }});
  document.getElementById('shown').textContent=shown;
}}
function filterToday(){{
  document.getElementById('d1').value=TODAY;
  document.getElementById('d2').value=TODAY;
  applyFilters();
}}
function resetF(){{
  document.getElementById('q').value='';
  document.getElementById('st').value='';
  document.getElementById('d1').value='';
  document.getElementById('d2').value='';
  applyFilters();
}}
document.addEventListener('DOMContentLoaded',function(){{
  ['q','st','d1','d2'].forEach(function(id){{
    var el=document.getElementById(id);
    el.addEventListener('input',applyFilters);
    el.addEventListener('change',applyFilters);
  }});
  applyFilters();
  setTimeout(function(){{location.reload()}}, 120000);
}});
"""


def esc(s):
    return html.escape(str(s or ""))


def mask_phone(phone):
    """+998977927504 -> +99897***7504 (ўртаси яширилади)."""
    p = str(phone or "").strip()
    if len(p) <= 10:
        return p
    return p[:6] + "***" + p[-4:]


def build_rop_page(rop_name, orders, updated_at):
    counts = {k: 0 for k in STATUS_ORDER}
    for o in orders:
        if o["status"] in counts:
            counts[o["status"]] += 1

    stats_html = f'<div class="stat"><div class="n">{len(orders)}</div><div class="l">Жами</div></div>'
    for key in STATUS_ORDER:
        emoji, label = STATUS_LABELS[key]
        stats_html += (f'<div class="stat"><div class="n">{counts[key]}</div>'
                       f'<div class="l">{emoji} {label}</div></div>')

    opts = '<option value="">Барча статус</option>'
    for key in STATUS_ORDER:
        emoji, label = STATUS_LABELS[key]
        opts += f'<option value="{key}">{emoji} {label}</option>'

    rows = []
    for o in orders:
        emoji, label = STATUS_LABELS.get(o["status"], ("", o["status"]))
        prods = "<br>".join(esc(p) for p in o["products"]) or "—"
        if o["phones"]:
            phones = "<br>".join(
                f'<span class="ph" data-full="{esc(p)}">'
                f'<span class="pm">{esc(mask_phone(p))}</span>'
                f'<button class="eye" onclick="togglePhone(this)" title="Кўрсатиш">👁</button>'
                f'</span>' for p in o["phones"])
        else:
            phones = "—"
        all_phones = " ".join(o["phones"])
        rows.append(f"""<tr data-status="{esc(o['status'])}" data-date="{esc(o['date'])}" data-phones="{esc(all_phones)}">
<td data-l="№" class="nowrap">{esc(o['order_num'])}</td>
<td data-l="Сана" class="nowrap">{esc(o['date'])}<div class="muted">{esc(o['time'])}</div></td>
<td data-l="Id сделки" class="nowrap">{esc(o['deal_id'])}</td>
<td data-l="Продукт" class="prod">{prods}</td>
<td data-l="Сумма" class="nowrap">{esc(o['summa'])}</td>
<td data-l="Регион">{esc(o['region'])}</td>
<td data-l="Адрес">{esc(o['address'])}</td>
<td data-l="Мижоз">{esc(o['client'])}</td>
<td data-l="Телефон" class="nowrap">{phones}</td>
<td data-l="Оператор">{esc(o['operator'])}<div class="muted">{esc(o['source'])}</div></td>
<td data-l="Статус"><span class="badge s-{esc(o['status'])}">{emoji} {label}</span></td>
</tr>""")

    return f"""<!DOCTYPE html>
<html lang="uz"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>{esc(rop_name)} — буюртмалар</title>
<style>{CSS}</style>
</head><body>
<h1>{esc(rop_name)} — буюртмалар</h1>
<div class="sub">Янгиланди: {updated_at} (ҳар 2 дақиқада автомат янгиланади) ·
Кўрсатилмоқда: <b id="shown">0</b> та</div>
<div class="stats">{stats_html}</div>
<div class="controls">
<input type="text" id="q" placeholder="Қидирув (исм, телефон, маҳсулот, ID...)">
<button class="btn btn-primary" onclick="filterToday()">📅 Бугун</button>
<select id="st">{opts}</select>
<input type="date" id="d1" title="Дан">
<input type="date" id="d2" title="Гача">
<button class="btn" onclick="resetF()">Тозалаш</button>
</div>
<div class="table-wrap">
<table><thead><tr>
<th>№</th><th>Сана</th><th>Id сделки</th><th>Продукт</th><th>Сумма</th>
<th>Регион</th><th>Адрес</th><th>Мижоз</th><th>Телефон</th><th>Оператор</th><th>Статус</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</div>
<script>{build_js(datetime.now(TZ).strftime('%Y-%m-%d'))}</script>
</body></html>"""


def build_index(by_rop, updated_at):
    cards = []
    for rop in sorted(by_rop.keys()):
        orders = by_rop[rop]
        counts = {k: 0 for k in STATUS_ORDER}
        for o in orders:
            if o["status"] in counts:
                counts[o["status"]] += 1
        fn = rop_filename(rop)
        cards.append(f"""<div class="rop-card">
<div class="name"><a href="{fn}">{esc(rop)}</a></div>
<div class="muted">Жами: <b>{len(orders)}</b> ·
✅ {counts['confirmed']} · 🟡 {counts['no_answer']} ·
❌ {counts['rejected']} · 🕔 {counts['confirm_new']}</div>
</div>""")

    return f"""<!DOCTYPE html>
<html lang="uz"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<title>РОП дашбоардлари</title>
<style>{CSS}</style>
</head><body>
<h1>РОП дашбоардлари</h1>
<div class="sub">Янгиланди: {updated_at}</div>
<div class="rop-list">{''.join(cards)}</div>
</body></html>"""


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    by_rop = load_orders()
    updated_at = datetime.now(TZ).strftime("%d.%m.%Y %H:%M")

    for rop, orders in by_rop.items():
        fn = rop_filename(rop)
        (OUT_DIR / fn).write_text(build_rop_page(rop, orders, updated_at), encoding="utf-8")
        print(f"{rop}: {len(orders)} ta buyurtma -> {fn}")

    (OUT_DIR / "index.html").write_text(build_index(by_rop, updated_at), encoding="utf-8")
    (OUT_DIR / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"\nJami {len(by_rop)} ta ROP dashboard yaratildi: {OUT_DIR}")


if __name__ == "__main__":
    main()
