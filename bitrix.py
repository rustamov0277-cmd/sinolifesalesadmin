# -*- coding: utf-8 -*-
"""
Bitrix24 REST API билан ишлаш — барча сўровлар шу ердан ўтади.

⚠️ МУҲИМ ТЕХНИК ҚАРОР: HTTP сўровлар Python'нинг urllib'и билан ЭМАС,
система curl'и орқали юборилади.

НЕГА: бу серверда Bitrix'га Python urllib орқали уланиш вақти-вақти билан
TLS handshake'да "қотиб" қоларди (`_ssl.c:983: The handshake operation
timed out`) — айни пайтда curl ўша сўровни 0.24 сонияда бажарарди.
Натижада poll 90 сония ўрнига 4 дақиқа давом этиб, навбатдагилари
ўтказиб юборилар, хабарлар соатлаб кечикарди.
Худди шу муаммо МойСклад интеграциясида ҳам бўлган ва curl билан ҳал қилинган.

Ҳар сўровда RETRY бор. Timeout'лар атайлаб КИЧИК: узилиш бўлса тезда
воз кечиб, кейинги poll'да қайта уриниш — узоқ кутишдан афзал
(маълумот йўқолмайди: poller.py since_iso'ни сурмайди).
"""
import json
import logging
import os
import subprocess
import time

import config

log = logging.getLogger("bitrix")

RETRY_COUNT = int(os.environ.get("SA_BX_RETRY", "3"))
RETRY_DELAY_SEC = float(os.environ.get("SA_BX_RETRY_DELAY", "1"))
TIMEOUT_SEC = int(os.environ.get("SA_BX_TIMEOUT", "10"))       # битта уриниш
CONNECT_TIMEOUT_SEC = int(os.environ.get("SA_BX_CONNECT_TIMEOUT", "5"))


def _bx(method, params=None):
    """Bitrix REST методини чақиради (curl орқали).
    Хатода {'error': ...} қайтаради — exception ОТМАЙДИ."""
    url = config.BITRIX_WEBHOOK + method + ".json"
    body = json.dumps(params or {})
    cmd = [
        "curl", "-sS", "--compressed",
        "--connect-timeout", str(CONNECT_TIMEOUT_SEC),
        "--max-time", str(TIMEOUT_SEC),
        "-w", "\n__HTTP__%{http_code}",
        "-H", "Content-Type: application/json",
        "--data-binary", "@-",
        url,
    ]

    last_err = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            res = subprocess.run(cmd, input=body, capture_output=True,
                                 text=True, timeout=TIMEOUT_SEC + 5)
            out = res.stdout or ""
            if "__HTTP__" not in out:
                last_err = (res.stderr or "curl javob bermadi").strip()[:200]
                log.warning("Bitrix %s: уриниш %d/%d муваффақиятсиз (%s)",
                            method, attempt, RETRY_COUNT, last_err)
                if attempt < RETRY_COUNT:
                    time.sleep(RETRY_DELAY_SEC)
                continue

            payload, code = out.rsplit("__HTTP__", 1)
            code = code.strip()

            if code == "200":
                try:
                    return json.loads(payload)
                except json.JSONDecodeError as e:
                    last_err = f"JSON parse: {e}"
                    log.error("Bitrix %s: жавобни ўқиб бўлмади: %s", method, payload[:200])
                    break

            # Bitrix'нинг ўз чеклови — узоқроқ кутиб қайта уринамиз
            if "QUERY_LIMIT_EXCEEDED" in payload:
                log.warning("Bitrix %s: QUERY_LIMIT_EXCEEDED, кутиб қайта уринилади.", method)
                last_err = "QUERY_LIMIT_EXCEEDED"
                time.sleep(RETRY_DELAY_SEC * 3)
                continue

            last_err = f"HTTP {code}: {payload[:200]}"
            log.warning("Bitrix %s: уриниш %d/%d — %s",
                        method, attempt, RETRY_COUNT, last_err)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY_SEC)

        except subprocess.TimeoutExpired:
            last_err = f"timeout ({TIMEOUT_SEC}s)"
            log.warning("Bitrix %s: уриниш %d/%d — timeout", method, attempt, RETRY_COUNT)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY_SEC)
        except Exception as e:
            last_err = str(e)
            log.error("Bitrix %s: кутилмаган хато: %s", method, e)
            break

    return {"error": "connection_failed", "error_description": str(last_err)}


def bx_call_list_all(method, params, result_key=None):
    """Bitrix'нинг 'start' пагинациясини тўлиқ айланиб чиқади.

    МУҲИМ: хато чиқса None қайтаради (бўш рўйхат [] эмас!) — токи
    "ҳеч нарса ўзгармаган" билан "Bitrix'га уланиб бўлмади"ни адаштирмасин.
    Чақирувчи (poller.py) буни кўриб, since_iso'ни СУРМАСЛИГИ керак —
    акс ҳолда узилиш пайтидаги ўзгаришлар абадий йўқолиб қолади."""
    out = []
    start = 0
    while True:
        p = dict(params)
        p["start"] = start
        resp = _bx(method, p)
        if "error" in resp:
            log.error("%s (list_all): %s", method, resp)
            return None  # ХАТО — [] эмас, аниқ "муваффақиятсиз" белгиси
        result = resp.get("result")
        batch = result.get(result_key) if result_key else result
        if not batch:
            break
        out.extend(batch)
        nxt = resp.get("next")
        if not nxt:
            break
        start = nxt
    return out


# ═══════════════════════ Сделка ═══════════════════════════════════════════

def bx_get_deal(deal_id):
    resp = _bx("crm.deal.get", {"id": deal_id})
    if "error" in resp:
        return None, resp.get("error_description", resp["error"])
    return resp.get("result"), None


def bx_get_deals_by_ids(deal_ids):
    """Бир нечта сделкани ID рўйхати бўйича олади (стадия ўзгаришини текшириш учун)."""
    if not deal_ids:
        return {}
    resp = _bx("crm.deal.list", {
        "filter": {"ID": list(deal_ids)},
        "select": ["ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "OPPORTUNITY", "SOURCE_ID",
                   "CONTACT_ID", "ASSIGNED_BY_ID", config.FIELD_REGION,
                   config.FIELD_ADDRESS, "DATE_MODIFY", "DATE_CREATE", "MOVED_TIME", "PREVIOUS_STAGE_ID",
                   "COMMENTS", config.FIELD_CONFIRM_ANALYSIS],
    })
    if "error" in resp:
        log.error("bx_get_deals_by_ids: %s", resp)
        return {}
    return {str(d["ID"]): d for d in resp.get("result", [])}


def bx_get_new_confirm_deals(since_iso):
    """'Тасдиқлаш' воронкасида C4:NEW стадиясига since_iso'дан кейин тушган сделкалар."""
    filt = {
        "CATEGORY_ID": config.CATEGORY_CONFIRM,
        "STAGE_ID": config.STAGE_CONFIRM_NEW,
    }
    if since_iso:
        filt[">DATE_MODIFY"] = since_iso
    return bx_call_list_all("crm.deal.list", {
        "filter": filt,
        "order": {"DATE_MODIFY": "ASC"},
        "select": ["ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "OPPORTUNITY", "SOURCE_ID",
                   "CONTACT_ID", "ASSIGNED_BY_ID", config.FIELD_REGION,
                   config.FIELD_ADDRESS, "DATE_MODIFY", "DATE_CREATE", "MOVED_TIME", "PREVIOUS_STAGE_ID",
                   "COMMENTS", config.FIELD_CONFIRM_ANALYSIS],
    })


def bx_get_recently_modified_tracked_deals(since_iso):
    """Кузатиладиган БАРЧА воронкаларда (Тасдиқлаш/Первичный/Доставка)
    ЖОРИЙ стадияга since_iso'дан кейин КЎЧГАН (MOVED_TIME) сделкалар.

    МУҲИМ: DATE_MODIFY эмас, МАХСУС MOVED_TIME ишлатилади — DATE_MODIFY
    сделканинг ИСТАЛГАН майдони (изоҳ, телефон ва ҳ.к.) ўзгарса ҳам
    янгиланади, бу эса эски сделкаларни хато равишда "янги стадия
    ўзгариши" деб кўрсатиб юборарди. MOVED_TIME эса ФАҚАТ стадия
    ҳақиқатан ўзгарганда янгиланади — тўғри мантиқ шу."""
    filt = {"CATEGORY_ID": config.TRACKED_CATEGORIES}
    if since_iso:
        filt[">MOVED_TIME"] = since_iso
    return bx_call_list_all("crm.deal.list", {
        "filter": filt,
        "order": {"MOVED_TIME": "ASC"},
        "select": ["ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "OPPORTUNITY", "SOURCE_ID",
                   "CONTACT_ID", "ASSIGNED_BY_ID", config.FIELD_REGION,
                   config.FIELD_ADDRESS, "DATE_MODIFY", "DATE_CREATE", "MOVED_TIME", "PREVIOUS_STAGE_ID",
                   "COMMENTS", config.FIELD_CONFIRM_ANALYSIS],
    })


def bx_get_deals_by_stages(category_id, stage_ids, since_iso):
    """Берилган category_id'даги, stage_ids рўйхатидан бирида турган,
    since_iso'дан кейин ЎЗГАРГАН сделкалар (бир марталик хабар учун)."""
    if not stage_ids:
        return []
    filt = {
        "CATEGORY_ID": category_id,
        "STAGE_ID": list(stage_ids),
    }
    if since_iso:
        filt[">MOVED_TIME"] = since_iso  # фақат стадия ҳақиқатан ўзгарганда (DATE_MODIFY эмас)
    return bx_call_list_all("crm.deal.list", {
        "filter": filt,
        "order": {"MOVED_TIME": "ASC"},
        "select": ["ID", "TITLE", "CATEGORY_ID", "STAGE_ID", "OPPORTUNITY", "SOURCE_ID",
                   "CONTACT_ID", "ASSIGNED_BY_ID", config.FIELD_REGION,
                   config.FIELD_ADDRESS, "DATE_MODIFY", "DATE_CREATE", "MOVED_TIME", "PREVIOUS_STAGE_ID",
                   "COMMENTS", config.FIELD_CONFIRM_ANALYSIS],
    })


def bx_get_deal_productrows(deal_id):
    resp = _bx("crm.deal.productrows.get", {"id": deal_id})
    if "error" in resp:
        log.error("bx_get_deal_productrows(%s): %s", deal_id, resp)
        return []
    return resp.get("result", [])


# ═══════════════════════ Контакт ═══════════════════════════════════════════

def bx_get_contact(contact_id):
    """Мижоз исми ва телефон(лар)ини қайтаради: (full_name, [phone1, phone2, ...])."""
    if not contact_id:
        return "", []
    resp = _bx("crm.contact.get", {"id": contact_id})
    if "error" in resp:
        return "", []
    c = resp.get("result") or {}
    full_name = ((c.get("NAME") or "") + " " + (c.get("LAST_NAME") or "")).strip()
    phones = []
    for p in (c.get("PHONE") or []):
        val = (p.get("VALUE") or "").strip()
        if val:
            phones.append(val)
    return full_name, phones


# ═══════════════════════ Ходим / РОП ════════════════════════════════════════

def bx_get_user(bitrix_id):
    resp = _bx("user.get", {"ID": bitrix_id})
    if "error" in resp:
        return None
    res = resp.get("result") or []
    return res[0] if res else None


def get_employee_number(bitrix_user):
    """Ходим email'идан рақамни ажратади: '119@sinolifemanager.uz' -> '119'."""
    if not bitrix_user:
        return ""
    email = bitrix_user.get("EMAIL", "") or ""
    domain_suffix = "@" + config.EMPLOYEE_EMAIL_DOMAIN
    if email.endswith(domain_suffix):
        return email[: -len(domain_suffix)]
    return ""


_depts_cache = {"ts": 0, "depts": []}


def bx_get_departments():
    now = time.time()
    if _depts_cache["depts"] and now - _depts_cache["ts"] < 6 * 3600:
        return _depts_cache["depts"]
    resp = _bx("department.get", {})
    depts = resp.get("result", []) if "error" not in resp else []
    if depts:
        _depts_cache["depts"] = depts
        _depts_cache["ts"] = now
    return depts or _depts_cache["depts"]


_sources_cache = {"ts": 0, "map": {}}


def bx_get_source_name(source_id):
    """SOURCE_ID кодини (масалан 'ADVERTISING') инсон ўқийдиган номга айлантиради."""
    if not source_id:
        return ""
    now = time.time()
    if not _sources_cache["map"] or now - _sources_cache["ts"] > 6 * 3600:
        resp = _bx("crm.status.list", {"filter": {"ENTITY_ID": "SOURCE"}})
        if "error" not in resp:
            _sources_cache["map"] = {s["STATUS_ID"]: s["NAME"] for s in resp.get("result", [])}
            _sources_cache["ts"] = now
    return _sources_cache["map"].get(source_id, source_id)


_category_stages_cache = {}  # category_id -> {"ts": ..., "stages": {stage_id: name}}


def bx_get_stages_for_category(category_id):
    """Берилган category_id'даги ҳамма стадияларни {stage_id: name} кўринишида
    қайтаради (6 соат кэш)."""
    now = time.time()
    cached = _category_stages_cache.get(category_id)
    if cached and now - cached["ts"] < 6 * 3600:
        return cached["stages"]
    resp = _bx("crm.dealcategory.stage.list", {"id": category_id})
    stages = {}
    if "error" not in resp:
        for s in resp.get("result", []):
            stages[s["STATUS_ID"]] = s["NAME"]
    if stages:
        _category_stages_cache[category_id] = {"ts": now, "stages": stages}
        return stages
    return cached["stages"] if cached else {}


def bx_get_stage_name(category_id, stage_id):
    return bx_get_stages_for_category(category_id).get(stage_id, stage_id)


def find_rop_for_department(dept_id, depts):
    """Бўлимдан юқорига қараб '(ROP)' деб номланган бўлимни қидиради."""
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


def resolve_rop_for_user(bitrix_user):
    """Ходимнинг РОПини топади (department -> '(ROP)' бўлими -> UF_HEAD)."""
    if not bitrix_user:
        return None
    depts = bx_get_departments()
    for dept_id in (bitrix_user.get("UF_DEPARTMENT") or []):
        rop = find_rop_for_department(dept_id, depts)
        if rop:
            return rop
    return None
