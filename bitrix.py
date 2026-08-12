# -*- coding: utf-8 -*-
"""
Bitrix24 REST API билан ишлаш — барча сўровлар шу ердан ўтади.

МУҲИМ: Bitrix TLS баъзан "қотиб" қолади (TCP уланади, handshake тугамайди).
Шунинг учун ҳар сўровда RETRY бор (3 марта, орасида кутиш билан).
"""
import json
import logging
import time
import urllib.request
import urllib.error
import socket

import config

log = logging.getLogger("bitrix")

RETRY_COUNT = 3
RETRY_DELAY_SEC = 2
TIMEOUT_SEC = 20


def _bx(method, params=None):
    """Bitrix REST методини чақиради. Хатода {'error': ...} қайтаради (exception эмас)."""
    url = config.BITRIX_WEBHOOK + method + ".json"
    data = json.dumps(params or {}).encode("utf-8")
    last_err = None
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            last_err = e
            log.warning("Bitrix %s: уриниш %d/%d муваффақиятсиз (%s)",
                        method, attempt, RETRY_COUNT, e)
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_DELAY_SEC)
        except Exception as e:
            last_err = e
            log.error("Bitrix %s: кутилмаган хато: %s", method, e)
            break
    return {"error": "connection_failed", "error_description": str(last_err)}


def bx_call_list_all(method, params, result_key=None):
    """Bitrix'нинг 'start' пагинациясини тўлиқ айланиб чиқади."""
    out = []
    start = 0
    while True:
        p = dict(params)
        p["start"] = start
        resp = _bx(method, p)
        if "error" in resp:
            log.error("%s (list_all): %s", method, resp)
            break
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
                   config.FIELD_ADDRESS, "DATE_MODIFY"],
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
                   config.FIELD_ADDRESS, "DATE_MODIFY"],
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