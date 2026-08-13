# -*- coding: utf-8 -*-
"""
Барча "хотира" шу файлда — JSON кўринишида дискда сақланади (бот қайта ишга
тушса ҳам йўқолмайди). Ёзиш ATOMIC (tmp файлга ёзиб, keyin rename) — ярим
ёзилган файл қолмаслиги учун.
"""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

TZ = timezone(timedelta(hours=5))  # Тошкент

BASE_DIR = Path(os.environ.get("SA_STATE_DIR", str(Path.home() / "sinolifesalesadmin")))
BASE_DIR.mkdir(parents=True, exist_ok=True)

DEAL_STATE_FILE = BASE_DIR / "deal_state.json"        # deal_id -> {...}
ORDER_COUNTERS_FILE = BASE_DIR / "order_counters.json"  # chat_id -> {date, counter}
ROP_GROUPS_FILE = BASE_DIR / "rop_groups.json"          # rop_bitrix_id -> chat_id
POLL_META_FILE = BASE_DIR / "poll_meta.json"            # {"last_poll_iso": ...}
AGGREGATE_FILE = BASE_DIR / "aggregate_channel.json"     # {"chat_id": ...}
DELIVERY_GROUPS_FILE = BASE_DIR / "delivery_stage_groups.json"  # stage_id -> chat_id
PENDING_NO_CHANNEL_FILE = BASE_DIR / "pending_no_channel.json"  # [deal_id, ...]


def _load(path, default):
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def _save(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)  # atomic rename


def now_tz():
    return datetime.now(TZ)


# ═══════════════════════ Сделка ↔ хабар ═════════════════════════════════════

def load_deal_state():
    return _load(DEAL_STATE_FILE, {})


def save_deal_state(data):
    _save(DEAL_STATE_FILE, data)


def get_deal_entry(deal_id):
    return load_deal_state().get(str(deal_id))


def upsert_deal_entry(deal_id, entry):
    data = load_deal_state()
    data[str(deal_id)] = entry
    save_deal_state(data)


def tracked_open_deal_ids():
    """Ҳали якуний статусга етмаган (яна ўзгариши мумкин) сделкалар ID рўйхати."""
    data = load_deal_state()
    return [did for did, e in data.items() if not e.get("terminal")]


# ═══════════════════════ Кунлик буюртма счётчиги (канал бўйича) ════════════

def next_order_number(chat_id):
    """Шу канал учун бугунги кетма-кет рақамни қайтаради (кун бошида 1'дан)."""
    data = _load(ORDER_COUNTERS_FILE, {})
    key = str(chat_id)
    today = now_tz().strftime("%Y-%m-%d")
    entry = data.get(key)
    if not entry or entry.get("date") != today:
        entry = {"date": today, "counter": 0}
    entry["counter"] += 1
    data[key] = entry
    _save(ORDER_COUNTERS_FILE, data)
    return entry["counter"]


# ═══════════════════════ РОП ↔ канал ════════════════════════════════════════

def load_rop_groups():
    return _load(ROP_GROUPS_FILE, {})


def save_rop_groups(data):
    _save(ROP_GROUPS_FILE, data)


def set_rop_group(rop_bitrix_id, chat_id):
    data = load_rop_groups()
    data[str(rop_bitrix_id)] = str(chat_id)
    save_rop_groups(data)


def get_rop_chat_id(rop_bitrix_id):
    if not rop_bitrix_id:
        return None
    return load_rop_groups().get(str(rop_bitrix_id))


# ═══════════════════════ Poll мета (охирги текширув вақти) ═════════════════

def get_last_poll_iso():
    return _load(POLL_META_FILE, {}).get("last_poll_iso")


def set_last_poll_iso(iso_str):
    _save(POLL_META_FILE, {"last_poll_iso": iso_str})


# ═══════════════════════ Умумий (барча РОПлар) канали ═══════════════════════

def get_aggregate_chat_id():
    return _load(AGGREGATE_FILE, {}).get("chat_id")


def set_aggregate_chat_id(chat_id):
    _save(AGGREGATE_FILE, {"chat_id": str(chat_id)})


# ═══════════════════════ Доставка стадияси ↔ гуруҳ (бир марталик хабар) ═════

def get_delivery_stage_groups():
    return _load(DELIVERY_GROUPS_FILE, {})


def set_delivery_stage_group(stage_id, chat_id):
    data = get_delivery_stage_groups()
    data[stage_id] = str(chat_id)
    _save(DELIVERY_GROUPS_FILE, data)


def remove_delivery_stage_group(stage_id):
    data = get_delivery_stage_groups()
    if stage_id in data:
        del data[stage_id]
        _save(DELIVERY_GROUPS_FILE, data)
        return True
    return False


# ═══════════════════════ Канали топилмаган сделкалар (қайта уриниш учун) ════

def get_pending_no_channel():
    return _load(PENDING_NO_CHANNEL_FILE, [])


def add_pending_no_channel(deal_id):
    data = get_pending_no_channel()
    deal_id = str(deal_id)
    if deal_id not in data:
        data.append(deal_id)
        _save(PENDING_NO_CHANNEL_FILE, data)


def remove_pending_no_channel(deal_id):
    data = get_pending_no_channel()
    deal_id = str(deal_id)
    if deal_id in data:
        data.remove(deal_id)
        _save(PENDING_NO_CHANNEL_FILE, data)