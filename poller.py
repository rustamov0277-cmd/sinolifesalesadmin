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

TERMINAL_STATUS_KEYS = {"confirmed"}
# Диққат: "rejected" (❌ Тасдиқланмади) ЯКУНИЙ ҳисобланмайди — сотувчи кейинчалик
# сделкани қайта кўриб чиқиб бошқа стадияга ўтказиши мумкин (масалан яна
# тасдиқлаш жараёнига қайтариши), бот буни ҳам кузатишда давом этади ва
# ўша хабарни янгилайди (48 соатгача edit, ундан кейин янги хабар — pastdagi
# TELEGRAM_EDIT_LIMIT_HOURS mantig'i orqali avtomat ishlaydi).


async def _resolve_channel_and_operator(deal):
    """Сделкага бириктирилган ходимдан РОП каналини, РОП номини ва оператор
    исмини топади."""
    assigned_id = deal.get("ASSIGNED_BY_ID")
    bitrix_user = bitrix.bx_get_user(assigned_id) if assigned_id else None
    operator_name = ""
    employee_number = ""
    chat_id = None
    rop_name = ""
    if bitrix_user:
        operator_name = ((bitrix_user.get("NAME") or "") + " " +
                          (bitrix_user.get("LAST_NAME") or "")).strip()
        employee_number = bitrix.get_employee_number(bitrix_user)
        rop = bitrix.resolve_rop_for_user(bitrix_user)
        if rop:
            chat_id = state.get_rop_chat_id(rop.get("head_bitrix_id"))
            rop_name = (rop.get("name") or "").replace("(ROP)", "").strip()
    return chat_id, operator_name, employee_number, rop_name


def _with_rop_header(text, rop_name):
    """Умумий канал учун — хабарнинг бошига РОП номини қўшади."""
    if not rop_name:
        return text
    return f"👥 РОП: {rop_name}\n\n" + text


async def _send_new_deal(bot, deal):
    deal_id = str(deal["ID"])
    chat_id, operator_name, employee_number, rop_name = await _resolve_channel_and_operator(deal)

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
    source_name = bitrix.bx_get_source_name(deal.get("SOURCE_ID"))

    order_num = state.next_order_number(chat_id)
    status_key = "confirm_new"

    text = message_format.build_order_message(
        order_num=order_num, deal_id=deal_id, products_rows=products_rows,
        summa=summa, region_name=region_name, address=address,
        client_name=client_name, phones=phones, operator_name=operator_name,
        employee_number=employee_number, status_key=status_key,
        source_name=source_name)

    try:
        msg = await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        log.error("Сделка %s: хабар юборилмади: %s", deal_id, e)
        return

    # ── Умумий каналга ҳам нусхаси (РОП номи билан) ─────────────────────
    agg_chat_id = state.get_aggregate_chat_id()
    agg_message_id = None
    if agg_chat_id:
        try:
            agg_msg = await bot.send_message(chat_id=agg_chat_id,
                                              text=_with_rop_header(text, rop_name))
            agg_message_id = agg_msg.message_id
        except Exception as e:
            log.error("Сделка %s: умумий каналга юборилмади: %s", deal_id, e)

    sheet_rows = sheets.log_new_order(
        order_num=order_num, deal_id=deal_id, products_rows=products_rows,
        summa=summa, region_name=region_name, address=address,
        client_name=client_name, phones=phones, operator_name=operator_name,
        employee_number=employee_number, status_key=status_key)

    state.upsert_deal_entry(deal_id, {
        "chat_id": chat_id,
        "message_id": msg.message_id,
        "agg_chat_id": agg_chat_id,
        "agg_message_id": agg_message_id,
        "rop_name": rop_name,
        "category": str(deal.get("CATEGORY_ID")),
        "stage": deal.get("STAGE_ID"),
        "status_key": status_key,
        "order_num": order_num,
        "sent_at": state.now_tz().isoformat(),
        "last_text": text,
        "sheet_rows": sheet_rows,
        "terminal": False,
    })
    log.info("Сделка %s: янги хабар юборилди (канал %s, №%03d).", deal_id, chat_id, order_num)


async def _update_existing_deal(bot, deal_id, entry, fresh_deal):
    category = str(fresh_deal.get("CATEGORY_ID"))
    stage = fresh_deal.get("STAGE_ID")
    status_key = config.STAGE_TO_STATUS_KEY.get((category, stage))

    if status_key is None:
        # Кузатилмайдиган стадия (масалан "Смс zextra тастиклаш") — хабарни
        # ЎЗГАРТИРМАЙМИЗ, лекин жорий стадияни сақлаб қоламиз (такрор текширмаслик учун)
        if category != entry.get("category") or stage != entry.get("stage"):
            entry["category"] = category
            entry["stage"] = stage
            state.upsert_deal_entry(deal_id, entry)
        return

    # ── Бутун хабарни Bitrix'даги ЭНГ СЎНГГИ маълумот билан қайта қурамиз ──
    # (фақат статус эмас — сумма, маҳсулот, манзил ва ҳ.к. ҳам ўзгарган бўлиши мумкин)
    region_id = str(fresh_deal.get(config.FIELD_REGION) or "")
    region_name = config.REGION_NAME_BY_ID.get(region_id, "")
    address = fresh_deal.get(config.FIELD_ADDRESS) or ""
    summa = fresh_deal.get("OPPORTUNITY") or 0
    source_name = bitrix.bx_get_source_name(fresh_deal.get("SOURCE_ID"))

    contact_id = fresh_deal.get("CONTACT_ID")
    client_name, phones = bitrix.bx_get_contact(contact_id)
    products_rows = bitrix.bx_get_deal_productrows(deal_id)

    assigned_id = fresh_deal.get("ASSIGNED_BY_ID")
    bitrix_user = bitrix.bx_get_user(assigned_id) if assigned_id else None
    operator_name = ((bitrix_user.get("NAME") or "") + " " +
                      (bitrix_user.get("LAST_NAME") or "")).strip() if bitrix_user else ""
    employee_number = bitrix.get_employee_number(bitrix_user) if bitrix_user else ""

    new_text = message_format.build_order_message(
        order_num=entry["order_num"], deal_id=deal_id, products_rows=products_rows,
        summa=summa, region_name=region_name, address=address,
        client_name=client_name, phones=phones, operator_name=operator_name,
        employee_number=employee_number, status_key=status_key,
        source_name=source_name)

    if new_text == entry.get("last_text"):
        # Ҳеч нарса ўзгармаган (матн ҳам, статус ҳам) — фақат стадияни сақлаймиз
        entry["category"] = category
        entry["stage"] = stage
        state.upsert_deal_entry(deal_id, entry)
        return

    # ── Матн (статус ва/ёки маълумотлар) ўзгарди — хабарни таҳрирлаймиз ────
    chat_id = entry["chat_id"]
    message_id = entry["message_id"]

    sent_at = datetime.fromisoformat(entry["sent_at"])
    hours_passed = (state.now_tz() - sent_at).total_seconds() / 3600

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

    # ── Умумий каналдаги нусхасини ҳам янгилаймиз ───────────────────────
    agg_chat_id = entry.get("agg_chat_id")
    agg_message_id = entry.get("agg_message_id")
    if agg_chat_id:
        agg_text = _with_rop_header(new_text, entry.get("rop_name", ""))
        agg_edited = False
        if agg_message_id and hours_passed < config.TELEGRAM_EDIT_LIMIT_HOURS:
            try:
                await bot.edit_message_text(chat_id=agg_chat_id, message_id=agg_message_id,
                                             text=agg_text)
                agg_edited = True
            except Exception as e:
                log.warning("Сделка %s: умумий канал edit муваффақиятсиз (%s).", deal_id, e)
        if not agg_edited:
            try:
                agg_msg = await bot.send_message(chat_id=agg_chat_id, text=agg_text)
                entry["agg_message_id"] = agg_msg.message_id
            except Exception as e:
                log.error("Сделка %s: умумий каналга юборилмади: %s", deal_id, e)

    status_changed = status_key != entry.get("status_key")

    entry.update({
        "message_id": message_id,
        "category": category,
        "stage": stage,
        "status_key": status_key,
        "last_text": new_text,
        "terminal": status_key in TERMINAL_STATUS_KEYS,
    })
    state.upsert_deal_entry(deal_id, entry)
    if status_changed:
        sheets.update_status(entry.get("sheet_rows"), status_key)
    log.info("Сделка %s: хабар янгиланди (%s)%s", deal_id,
              "edit" if edited else "янги хабар",
              " — статус -> " + status_key if status_changed else " — маълумот ўзгарди")


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