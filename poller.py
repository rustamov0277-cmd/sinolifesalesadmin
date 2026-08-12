# -*- coding: utf-8 -*-
"""
Polling мантиқи. bot.py'даги job_queue ҳар config.POLL_INTERVAL_SECONDS'да
poll_once()ни чақиради.
"""
import logging
from datetime import datetime, timedelta

import config
import state
import bitrix
import message_format
import sheets

log = logging.getLogger("poller")

TERMINAL_STATUS_KEYS = {"rejected", "confirmed"}


async def _resolve_channel_and_operator(deal):
    """Сделкага бириктирилган ходимдан РОП каналини ва оператор исмини топади."""
    assigned_id = deal.get("ASSIGNED_BY_ID")
    bitrix_user = bitrix.bx_get_user(assigned_id) if assigned_id else None
    operator_name = ""
    employee_number = ""
    chat_id = None
    if bitrix_user:
        operator_name = ((bitrix_user.get("NAME") or "") + " " +
                          (bitrix_user.get("LAST_NAME") or "")).strip()
        employee_number = bitrix.get_employee_number(bitrix_user)
        rop = bitrix.resolve_rop_for_user(bitrix_user)
        if rop:
            chat_id = state.get_rop_chat_id(rop.get("head_bitrix_id"))
    return chat_id, operator_name, employee_number


async def _send_new_deal(bot, deal):
    deal_id = str(deal["ID"])
    chat_id, operator_name, employee_number = await _resolve_channel_and_operator(deal)

    if not chat_id:
        log.warning("Сделка %s: РОП канали топилмади (/addropgroup билан қўшилмаган).", deal_id)
        for aid in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=aid,
                    text=("⚠️ Сделка #" + deal_id + " учун РОП канали топилмади.\n"
                          "/listrops билан РОПларни кўринг, /addropgroup билан бириктиринг."))
            except Exception as e:
                log.error("admin warn: %s", e)
        return  # кейинги poll'да қайта уринилади (state'га ёзилмайди)

    region_id = str(deal.get(config.FIELD_REGION) or "")
    region_name = config.REGION_NAME_BY_ID.get(region_id, "")
    address = deal.get(config.FIELD_ADDRESS) or ""
    summa = deal.get("OPPORTUNITY") or 0

    contact_id = deal.get("CONTACT_ID")
    client_name, phones = bitrix.bx_get_contact(contact_id)

    products_rows = bitrix.bx_get_deal_productrows(deal_id)

    order_num = state.next_order_number(chat_id)
    status_key = "confirm_new"

    text = message_format.build_order_message(
        order_num=order_num, deal_id=deal_id, products_rows=products_rows,
        summa=summa, region_name=region_name, address=address,
        client_name=client_name, phones=phones, operator_name=operator_name,
        employee_number=employee_number, status_key=status_key)

    try:
        msg = await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        log.error("Сделка %s: хабар юборилмади: %s", deal_id, e)
        return

    sheet_row = sheets.log_new_order(
        order_num=order_num, deal_id=deal_id, products_rows=products_rows,
        summa=summa, region_name=region_name, address=address,
        client_name=client_name, phones=phones, operator_name=operator_name,
        employee_number=employee_number, status_key=status_key)

    state.upsert_deal_entry(deal_id, {
        "chat_id": chat_id,
        "message_id": msg.message_id,
        "category": str(deal.get("CATEGORY_ID")),
        "stage": deal.get("STAGE_ID"),
        "status_key": status_key,
        "order_num": order_num,
        "sent_at": state.now_tz().isoformat(),
        "last_text": text,
        "sheet_row": sheet_row,
        "terminal": False,
    })
    log.info("Сделка %s: янги хабар юборилди (канал %s, №%03d).", deal_id, chat_id, order_num)


async def _update_existing_deal(bot, deal_id, entry, fresh_deal):
    category = str(fresh_deal.get("CATEGORY_ID"))
    stage = fresh_deal.get("STAGE_ID")

    if category == entry.get("category") and stage == entry.get("stage"):
        return  # ўзгариш йўқ

    status_key = config.STAGE_TO_STATUS_KEY.get((category, stage))

    if status_key is None:
        # Кузатилмайдиган стадия (масалан "Смс zextra тастиклаш") — хабарни
        # ЎЗГАРТИРМАЙМИЗ, лекин жорий стадияни сақлаб қоламиз (такрор текширмаслик учун)
        entry["category"] = category
        entry["stage"] = stage
        state.upsert_deal_entry(deal_id, entry)
        return

    if status_key == entry.get("status_key"):
        entry["category"] = category
        entry["stage"] = stage
        state.upsert_deal_entry(deal_id, entry)
        return

    # ── Статус ҳақиқатан ўзгарди — хабарни таҳрирлаймиз ──────────────────
    chat_id = entry["chat_id"]
    message_id = entry["message_id"]

    sent_at = datetime.fromisoformat(entry["sent_at"])
    hours_passed = (state.now_tz() - sent_at).total_seconds() / 3600

    new_text = message_format.replace_status_line(
        _cached_text_or_rebuild(entry, fresh_deal), status_key)

    edited = False
    if hours_passed < config.TELEGRAM_EDIT_LIMIT_HOURS:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=new_text)
            edited = True
        except Exception as e:
            log.warning("Сделка %s: edit муваффақиятсиз (%s) — янги хабар юбораман.", deal_id, e)

    if not edited:
        # 48 соатдан ошган ёки edit хато берди -> янги хабар
        try:
            msg = await bot.send_message(chat_id=chat_id, text=new_text)
            message_id = msg.message_id
            entry["sent_at"] = state.now_tz().isoformat()
        except Exception as e:
            log.error("Сделка %s: fallback хабар ҳам юборилмади: %s", deal_id, e)
            return

    entry.update({
        "message_id": message_id,
        "category": category,
        "stage": stage,
        "status_key": status_key,
        "last_text": new_text,
        "terminal": status_key in TERMINAL_STATUS_KEYS,
    })
    state.upsert_deal_entry(deal_id, entry)
    sheets.update_status(entry.get("sheet_row"), status_key)
    log.info("Сделка %s: статус -> %s (%s)", deal_id, status_key,
              "edit" if edited else "янги хабар")


def _cached_text_or_rebuild(entry, fresh_deal):
    """Матнни қайта қуриш учун асос — таҳрирлашда фақат сўнгги қатор алмашгани учун
    олдинги матнни сақлаб қўямиз (last_text), топилмаса содда fallback қурамиз."""
    if entry.get("last_text"):
        return entry["last_text"]
    # fallback: жуда содда матн (амалда деярли ишлатилмайди, чунки янги сделкада
    # last_text = build_order_message натижаси дарҳол сақланади)
    return message_format.build_order_message(
        order_num=entry.get("order_num", 0), deal_id=entry.get("deal_id", "?"),
        products_rows=[], summa=fresh_deal.get("OPPORTUNITY") or 0,
        region_name="", address="", client_name="", phones=[],
        operator_name="", employee_number="", status_key=entry["status_key"])


async def poll_once(bot):
    since_iso = state.get_last_poll_iso()
    poll_started_at = state.now_tz().isoformat()

    # 1) Янги C4:NEW сделкалар
    new_deals = bitrix.bx_get_new_confirm_deals(since_iso)
    for deal in new_deals:
        deal_id = str(deal["ID"])
        if state.get_deal_entry(deal_id):
            continue  # аллақачон юборилган
        await _send_new_deal(bot, deal)

    # 2) Кузатилаётган (якунланмаган) сделкаларнинг жорий стадиясини текшириш
    open_ids = state.tracked_open_deal_ids()
    if open_ids:
        fresh_map = bitrix.bx_get_deals_by_ids(open_ids)
        for deal_id in open_ids:
            entry = state.get_deal_entry(deal_id)
            fresh_deal = fresh_map.get(deal_id)
            if not entry or not fresh_deal:
                continue
            await _update_existing_deal(bot, deal_id, entry, fresh_deal)

    state.set_last_poll_iso(poll_started_at)