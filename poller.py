# -*- coding: utf-8 -*-
"""
Polling мантиқи. bot.py'даги job_queue ҳар config.POLL_INTERVAL_SECONDS'да
poll_once()ни чақиради.

ЮК (150+ буюртма/кун) остида хавфсиз ишлаши учун:
  - Барча Bitrix (синхрон) сўровлар asyncio.to_thread'да чақирилади —
    event loop ҳеч қачон БЛОКЛАНМАЙДИ (Telegram буйруқлари, edit'лар
    ва бошқа poll'лар паралел давом этаверади).
  - Бир сделкани қайта ишлашда хато чиқса, ФАҚАТ ўша сделка ўтказиб
    юборилади — қолганлари давом этади (try/except ҳар сделка учун алоҳида).
  - deal_state.json БИР МАРТА ўқилади, хотирада ўзгартирилади, БИР МАРТА
    ёзилади (ҳар сделка учун алоҳида файл ўқиш/ёзиш эмас).
  - Очиқ сделкалар config.MAX_CONCURRENT_BITRIX чегарасида параллел
    (thread pool орқали) қайта ишланади — Bitrix'ни "QUERY_LIMIT_EXCEEDED"
    билан урмаслик учун чегараланган.
  - "Тасдиқланмади" сделкалар config.REJECTED_TRACK_DAYS кундан кейин
    автомат "терминал" деб белгиланади — чексиз тўпланиб қолмаслиги учун.
"""
import asyncio
import logging
from datetime import datetime

import config
import state
import bitrix
import message_format
import sheets

log = logging.getLogger("poller")

TERMINAL_STATUS_KEYS = {"confirmed"}
# Диққат: "rejected" (❌ Тасдиқланмади) дарҳол ЯКУНИЙ ҳисобланмайди — сотувчи
# кейинчалик сделкани қайта кўриб чиқиши мумкин, лекин config.REJECTED_TRACK_DAYS
# кундан кейин (пастда, poll_once охирида) АВТОМАТ терминал қилиб қўйилади.


# ═══════════════════════ Bitrix'ни thread'да чақириш (event loop'ни блокламаслик учун) ═

async def _bx_call(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


# ═══════════════════════ Ёрдамчилар ══════════════════════════════════════

async def _resolve_channel_and_operator(deal):
    """Сделкага бириктирилган ходимдан РОП каналини, РОП номини ва оператор
    исмини топади."""
    assigned_id = deal.get("ASSIGNED_BY_ID")
    bitrix_user = await _bx_call(bitrix.bx_get_user, assigned_id) if assigned_id else None
    operator_name = ""
    employee_number = ""
    chat_id = None
    rop_name = ""
    if bitrix_user:
        operator_name = ((bitrix_user.get("NAME") or "") + " " +
                          (bitrix_user.get("LAST_NAME") or "")).strip()
        employee_number = bitrix.get_employee_number(bitrix_user)
        rop = await _bx_call(bitrix.resolve_rop_for_user, bitrix_user)
        if rop:
            chat_id = state.get_rop_chat_id(rop.get("head_bitrix_id"))
            rop_name = (rop.get("name") or "").replace("(ROP)", "").strip()
    return chat_id, operator_name, employee_number, rop_name


def _resolve_status_key(category, stage, previous_stage):
    """STAGE_TO_STATUS_KEY'дан статусни аниқлайди.

    ЭСКИ МАНТИҚ (олиб ташланди): "Тасдикланмаган" фақат аниқ C4:LOSE'дан
    келганда қабул қилинарди — бу ЖУДА қаттиқ бўлиб чиқди: Bitrix
    автоматикаси ҳар доим ҳам PREVIOUS_STAGE_ID'ни бир хил қилиб
    қолдирмайди, натижада ҳақиқий "Тасдиқланмади" ўтишлар хабарсиз
    қолиб кетарди. Эски сделкаларнинг тасодифан хабар беришидан ҳимоя
    учун MOVED_TIME филтри (bitrix.py) ёлғиз ўзи етарли — шунинг учун
    бу ерда previous_stage'ни энди текширмаймиз."""
    return config.STAGE_TO_STATUS_KEY.get((category, stage))


def _with_rop_header(text, rop_name):
    """Умумий канал учун — хабарнинг бошига РОП номини қўшади."""
    if not rop_name:
        return text
    return f"👥 РОП: {rop_name}\n\n" + text


async def _alert_admins_delivery_failed(bot, deal_id, phones, reason):
    """Telegram'га хабар (янги ёки таҳрирланган) ҳеч қандай усулда
    ЮБОРИЛМАГАНДА — админларга дарҳол, телефон рақами билан хабар беради."""
    phone_str = " / ".join([p for p in (phones or []) if p]) or "—"
    text = (f"🚨 Telegram'га хабар ЮБОРИЛМАДИ!\n"
            f"Сделка: #{deal_id}\n"
            f"📞 Телефон: {phone_str}\n"
            f"Сабаб: {reason}")
    for aid in config.ADMIN_IDS:
        try:
            await bot.send_message(chat_id=aid, text=text)
        except Exception as e:
            log.error("Админга delivery-fail огоҳлантириши юборилмади (%s): %s", aid, e)


async def _fetch_deal_content(deal_id, deal_like):
    """Сделка учун контакт/маҳсулот/манба маълумотини (параллел, thread'да) олади."""
    contact_id = deal_like.get("CONTACT_ID")
    contact_task = _bx_call(bitrix.bx_get_contact, contact_id)
    products_task = _bx_call(bitrix.bx_get_deal_productrows, deal_id)
    source_task = _bx_call(bitrix.bx_get_source_name, deal_like.get("SOURCE_ID"))
    (client_name, phones), products_rows, source_name = await asyncio.gather(
        contact_task, products_task, source_task)
    return client_name, phones, products_rows, source_name


# ═══════════════════════ Янги сделка ═══════════════════════════════════════

async def _build_new_deal_entry(bot, deal, status_key="confirm_new"):
    """Кузатиладиган воронкаларда (ҳозир status_key стадиясида турган) ЯНГИ
    сделка учун хабар тайёрлайди, юборади ва ёзув (entry) қайтаради. Канал
    топилмаса None қайтаради (deal_state'га ёзилмайди, кейинги poll'да
    қайта уринилади)."""
    deal_id = str(deal["ID"])
    chat_id, operator_name, employee_number, rop_name = await _resolve_channel_and_operator(deal)

    if not chat_id:
        contact_id = deal.get("CONTACT_ID")
        try:
            client_name, phones = await _bx_call(bitrix.bx_get_contact, contact_id)
        except Exception:
            client_name, phones = "", []
        phone_str = " / ".join(phones[:2]) if phones else "—"
        already_pending = deal_id in state.get_pending_no_channel()
        state.add_pending_no_channel(deal_id)  # ҳар poll'да қайта уринилади
        log.warning("Сделка %s: РОП канали топилмади (/addropgroup билан қўшилмаган).", deal_id)
        if not already_pending:  # фақат БИРИНЧИ марта огоҳлантирамиз (спам бўлмаслиги учун)
            for aid in config.ADMIN_IDS:
                try:
                    await bot.send_message(
                        chat_id=aid,
                        text=("⚠️ Сделка #" + deal_id + " учун РОП канали топилмади.\n"
                              f"👤 Мижоз: {client_name or '—'}\n"
                              f"📞 Телефон: {phone_str}\n\n"
                              "/listrops билан РОПларни кўринг, /addropgroup билан бириктиринг.\n"
                              "(Канал қўшилгач, бот АВТОМАТ қайта уринади — қўшимча ҳаракат керакмас.)"))
                except Exception as e:
                    log.error("admin warn: %s", e)
        return None

    region_id = str(deal.get(config.FIELD_REGION) or "")
    region_name = config.REGION_NAME_BY_ID.get(region_id, "")
    address = deal.get(config.FIELD_ADDRESS) or ""
    summa = deal.get("OPPORTUNITY") or 0

    client_name, phones, products_rows, source_name = await _fetch_deal_content(deal_id, deal)

    order_num = state.next_order_number(chat_id)

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
        await _alert_admins_delivery_failed(bot, deal_id, phones, str(e))
        return None

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

    try:
        sheet_rows = await _bx_call(
            sheets.log_new_order,
            order_num=order_num, deal_id=deal_id, products_rows=products_rows,
            summa=summa, region_name=region_name, address=address,
            client_name=client_name, phones=phones, operator_name=operator_name,
            employee_number=employee_number, status_key=status_key,
            rop_name=rop_name, source_name=source_name)
    except Exception as e:
        log.error("Сделка %s: Sheets'га ёзилмади: %s", deal_id, e)
        sheet_rows = []

    now_iso = state.now_tz().isoformat()
    log.info("Сделка %s: янги хабар юборилди (канал %s, №%03d).", deal_id, chat_id, order_num)
    state.remove_pending_no_channel(deal_id)  # энди топилди — кутиш рўйхатидан чиқарамиз

    return {
        "chat_id": chat_id,
        "message_id": msg.message_id,
        "agg_chat_id": agg_chat_id,
        "agg_message_id": agg_message_id,
        "rop_name": rop_name,
        "category": str(deal.get("CATEGORY_ID")),
        "stage": deal.get("STAGE_ID"),
        "status_key": status_key,
        "order_num": order_num,
        "created_at": now_iso,   # ҳеч қачон ўзгармайди — expiry ҳисоби учун
        "sent_at": now_iso,      # fallback'да қайта ёзилиши мумкин (edit chegarasi учун)
        "last_text": text,
        "sheet_rows": sheet_rows,
        "terminal": False,
    }


# ═══════════════════════ Мавжуд сделкани янгилаш ═══════════════════════════

async def _compute_updated_entry(bot, deal_id, entry, fresh_deal):
    """entry'ни (агар керак бўлса) янгилайди ва қайтаради. Ҳеч қандай
    ўзгариш бўлмаса ҳам entry'нинг ўзини (эҳтимол category/stage янгиланган
    ҳолда) қайтаради — чақирувчи доим entry'ни deal_state'га қайтаради."""
    category = str(fresh_deal.get("CATEGORY_ID"))
    stage = fresh_deal.get("STAGE_ID")
    status_key = _resolve_status_key(category, stage, fresh_deal.get("PREVIOUS_STAGE_ID"))

    if status_key is None:
        # Кузатилмайдиган стадия — хабарни ЎЗГАРТИРМАЙМИЗ, стадияни сақлаймиз
        entry["category"] = category
        entry["stage"] = stage
        return entry

    # ── 48 соатдан ошган бўлса — умуман кузатишни тўхтатамиз ────────────
    # (Telegram'да барибир edit қилиб бўлмайди, янги хабар ҳам юбормаймиз —
    # шунчаки шу сделкани "терминал" деб белгилаб, ортиқча Bitrix сўровидан
    # ва ортиқча хабардан қочамиз)
    sent_at = datetime.fromisoformat(entry["sent_at"])
    hours_passed = (state.now_tz() - sent_at).total_seconds() / 3600
    if hours_passed >= config.TELEGRAM_EDIT_LIMIT_HOURS:
        entry["category"] = category
        entry["stage"] = stage
        entry["status_key"] = status_key
        entry["terminal"] = True
        log.info("Сделка %s: 48 соатдан ошди — кузатиш тўхтатилди (edit имконсиз).", deal_id)
        return entry

    # ── Бутун хабарни Bitrix'даги ЭНГ СЎНГГИ маълумот билан қайта қурамиз ──
    region_id = str(fresh_deal.get(config.FIELD_REGION) or "")
    region_name = config.REGION_NAME_BY_ID.get(region_id, "")
    address = fresh_deal.get(config.FIELD_ADDRESS) or ""
    summa = fresh_deal.get("OPPORTUNITY") or 0

    client_name, phones, products_rows, source_name = await _fetch_deal_content(deal_id, fresh_deal)

    assigned_id = fresh_deal.get("ASSIGNED_BY_ID")
    bitrix_user = await _bx_call(bitrix.bx_get_user, assigned_id) if assigned_id else None
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
        entry["category"] = category
        entry["stage"] = stage
        return entry

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
        try:
            msg = await bot.send_message(chat_id=chat_id, text=new_text)
            message_id = msg.message_id
            entry["sent_at"] = state.now_tz().isoformat()
        except Exception as e:
            log.error("Сделка %s: fallback хабар ҳам юборилмади: %s", deal_id, e)
            await _alert_admins_delivery_failed(bot, deal_id, phones, str(e))
            await _bx_call(sheets.update_telegram_ok, entry.get("sheet_rows"), False)
            return entry  # ҳеч бўлмаса категория/стадия ўзгармаган ҳолда қолади

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

    if status_changed:
        try:
            await _bx_call(sheets.update_status, entry.get("sheet_rows"), status_key)
        except Exception as e:
            log.error("Сделка %s: Sheets статус янгиланмади: %s", deal_id, e)

    log.info("Сделка %s: хабар янгиланди (%s)%s", deal_id,
              "edit" if edited else "янги хабар",
              " — статус -> " + status_key if status_changed else " — маълумот ўзгарди")
    return entry


# ═══════════════════════ Асосий poll цикли ═════════════════════════════════

async def catchup_missed_deals(bot, days=1):
    """Қўлда чақириладиган "тўлдириш" — сўнгги N кунда ЎЗГАРГАН, лекин ҳали
    deal_state'да ЙЎҚ (ботдан ўтказиб юборилган) сделкаларни топиб, ҳозирги
    ҳолатидан бошлаб хабар юборади. Одатий poll'дан фарқли — since_iso'ни
    эмас, N кунлик кенг ойнани ишлатади."""
    from datetime import timedelta
    since_dt = state.now_tz() - timedelta(days=days)
    since_iso = since_dt.isoformat()

    deal_state = state.load_deal_state()
    try:
        modified_deals = await _bx_call(bitrix.bx_get_recently_modified_tracked_deals, since_iso)
    except Exception as e:
        log.exception("catchup: сделкаларни олишда хато: %s", e)
        return 0, [("—", str(e))]

    sent_count = 0
    errors = []
    for deal in modified_deals:
        deal_id = str(deal["ID"])
        if deal_id in deal_state:
            continue
        category = str(deal.get("CATEGORY_ID"))
        stage = deal.get("STAGE_ID")
        status_key = _resolve_status_key(category, stage, deal.get("PREVIOUS_STAGE_ID"))
        if status_key is None:
            continue
        try:
            entry = await _build_new_deal_entry(bot, deal, status_key=status_key)
            if entry:
                deal_state[deal_id] = entry
                sent_count += 1
        except Exception as e:
            log.exception("catchup: сделка %s хато: %s", deal_id, e)
            errors.append((deal_id, str(e)))

    state.save_deal_state(deal_state)
    return sent_count, errors


async def _process_moy_sklad_tracking(since_iso, deal_state):
    """Сделка 'В пути' (C6:UC_4UD7I9) стадиясига тушганда, Sheets'даги
    "Moy_sklad" устунини 1'га белгилайди. Telegram хабари ЙЎҚ — фақат
    Sheets индикатори (сделка confirm-оқимидан ўтиб, sheet_rows'и бор
    бўлсагина ишлайди)."""
    try:
        deals = await _bx_call(bitrix.bx_get_deals_by_stages,
                                config.CATEGORY_DELIVERY,
                                [config.STAGE_DELIVERY_ON_THE_WAY], since_iso)
    except Exception as e:
        log.exception("'В пути' сделкаларини олишда хато: %s", e)
        return
    if deals is None:
        log.warning("'В пути' сделкаларини олиб бўлмади (Bitrix уланиш хатоси).")
        return

    for deal in deals:
        deal_id = str(deal["ID"])
        sheet_rows = deal_state.get(deal_id, {}).get("sheet_rows")
        if not sheet_rows:
            continue
        try:
            await _bx_call(sheets.update_moy_sklad_ok, sheet_rows, True)
            log.info("Сделка %s: 'В пути' — Moy_sklad=1 белгиланди.", deal_id)
        except Exception as e:
            log.error("Сделка %s: Moy_sklad янгиланмади: %s", deal_id, e)


async def poll_once(bot):
    deal_state = state.load_deal_state()  # БИР МАРТА ўқиймиз
    since_iso = state.get_last_poll_iso()
    poll_started_at = state.now_tz().isoformat()
    errors = []  # [(deal_id, xato_matni), ...] — poll охирида админга биргаликда юборилади

    # 0) Аввал "канал топилмади" деб қолган сделкаларни ҳар poll'да қайта
    #    синаймиз (масалан /addropgroup энди қўшилган бўлиши мумкин) —
    #    MOVED_TIME ўзгармаган бўлса ҳам, бу сделкалар ЎТКАЗИБ ЮБОРИЛМАЙДИ.
    pending_ids = state.get_pending_no_channel()
    if pending_ids:
        try:
            pending_map = await _bx_call(bitrix.bx_get_deals_by_ids, pending_ids)
        except Exception as e:
            log.exception("Pending сделкаларни олишда хато: %s", e)
            pending_map = {}
        for deal_id in pending_ids:
            deal = pending_map.get(deal_id)
            if not deal:
                continue  # сделка ўчирилган/топилмади — рўйхатда қолаверади
            category = str(deal.get("CATEGORY_ID"))
            stage = deal.get("STAGE_ID")
            status_key = _resolve_status_key(category, stage, deal.get("PREVIOUS_STAGE_ID"))
            if status_key is None:
                continue
            try:
                entry = await _build_new_deal_entry(bot, deal, status_key=status_key)
                if entry:
                    deal_state[deal_id] = entry
                    log.info("Сделка %s: pending рўйхатидан муваффақиятли юборилди.", deal_id)
            except Exception as e:
                log.exception("Сделка %s: pending қайта уринишда хато: %s", deal_id, e)
                errors.append((deal_id, str(e)))

    # 1) Кузатиладиган воронкаларда (Тасдиқлаш/Первичный/Доставка) since_iso'дан
    #    кейин ЎЗГАРГАН, лекин ҳали deal_state'да ЙЎҚ сделкалар — булар "янги"
    #    ёки "poll оралиғида тезда бир нечта стадияни сакраб ўтган" сделкалар.
    #    Ҳозир қаерда турса ҳам (C4:NEW бўлмаса ҳам), status_key аниқланса —
    #    шу нуқтадан бошлаб хабар яратилади (буюртма умуман ўтказиб юборилмайди).
    modified_deals = await _bx_call(bitrix.bx_get_recently_modified_tracked_deals, since_iso)
    fetch_ok = modified_deals is not None
    if not fetch_ok:
        # Bitrix'га уланиб бўлмади — since_iso'НИ СУРМАЙМИЗ, кейинги
        # poll'да ХУДДИ ШУ ойна яна текширилади (ўзгаришлар йўқолмайди)
        log.warning("Ўзгарган сделкаларни олиб бўлмади (Bitrix уланиш хатоси) — "
                    "since_iso сурилмайди, кейинги poll'да қайта уринилади.")
        errors.append(("—", "Ўзгарган сделкаларни олиб бўлмади (Bitrix уланиш хатоси)"))
        modified_deals = []

    for deal in modified_deals:
        deal_id = str(deal["ID"])
        category = str(deal.get("CATEGORY_ID"))
        stage = deal.get("STAGE_ID")
        status_key = _resolve_status_key(category, stage, deal.get("PREVIOUS_STAGE_ID"))

        if deal_id in deal_state:
            # Аллақачон кузатилган (ҳатто "терминал" бўлса ҳам) — лекин
            # MOVED_TIME ЯНГИ бўлгани учун шу ерга тушди, демак стадияси
            # ЯНА ўзгарган (масалан аввал "Тасдиқланди" бўлган сделка
            # орқага, C4:NEW'га қайтарилган). Терминал белгисидан қатъи
            # назар қайта текширамиз (жонлантириш).
            if status_key is None:
                continue  # ignored стадия — ўтказиб юборамиз
            entry = deal_state[deal_id]
            try:
                updated = await _compute_updated_entry(bot, deal_id, entry, deal)
                deal_state[deal_id] = updated
                if updated.get("status_key") != entry.get("status_key"):
                    log.info("Сделка %s: қайта жонлантирилди (терминалдан статус -> %s).",
                              deal_id, updated.get("status_key"))
            except Exception as e:
                log.exception("Сделка %s: қайта текширишда хато: %s", deal_id, e)
                errors.append((deal_id, str(e)))
            continue

        if status_key is None:
            continue  # ҳали кузатиладиган стадияда эмас (ignored stage) — кейинги poll'да текширилади

        try:
            entry = await _build_new_deal_entry(bot, deal, status_key=status_key)
            if entry:
                deal_state[deal_id] = entry
                if status_key != "confirm_new":
                    log.info("Сделка %s: %s стадиясидан тутиб олинди (poll оралиғида сакраб ўтган).",
                              deal_id, status_key)
        except Exception as e:
            log.exception("Сделка %s: янги хабар яратишда хато: %s", deal_id, e)
            errors.append((deal_id, str(e)))
            # шу сделка ўтказиб юборилади, қолганлари давом этади

    # 2) Кузатилаётган (якунланмаган) сделкалар — ПАРАЛЛЕЛ, лекин чегараланган
    open_ids = [did for did, e in deal_state.items() if not e.get("terminal")]
    if open_ids:
        try:
            fresh_map = await _bx_call(bitrix.bx_get_deals_by_ids, open_ids)
        except Exception as e:
            log.exception("Очиқ сделкаларни олишда хато: %s", e)
            errors.append(("—", f"Очиқ сделкаларни олишда хато: {e}"))
            fresh_map = {}

        semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_BITRIX)

        async def _process_one(deal_id):
            fresh_deal = fresh_map.get(deal_id)
            entry = deal_state.get(deal_id)
            if not entry or not fresh_deal:
                return
            async with semaphore:
                try:
                    updated = await _compute_updated_entry(bot, deal_id, entry, fresh_deal)
                    deal_state[deal_id] = updated
                except Exception as e:
                    log.exception("Сделка %s: янгилашда хато (ўтказиб юборилди): %s", deal_id, e)
                    errors.append((deal_id, str(e)))

        await asyncio.gather(*(_process_one(did) for did in open_ids))

    # 3) "Тасдиқланмади" сделкаларни муддати ўтгач автомат терминал қиламиз
    #    (чексиз тўпланиб қолмаслиги учун)
    now = state.now_tz()
    for deal_id, entry in deal_state.items():
        if entry.get("terminal") or entry.get("status_key") != "rejected":
            continue
        created_raw = entry.get("created_at") or entry.get("sent_at")
        try:
            created_dt = datetime.fromisoformat(created_raw)
        except (TypeError, ValueError):
            continue
        age_days = (now - created_dt).total_seconds() / 86400
        if age_days >= config.REJECTED_TRACK_DAYS:
            entry["terminal"] = True
            log.info("Сделка %s: %d кундан бери 'Тасдиқланмади' — кузатиш тўхтатилди.",
                      deal_id, config.REJECTED_TRACK_DAYS)

    state.save_deal_state(deal_state)  # БИР МАРТА ёзамиз (бу барибир сақланади)
    if fetch_ok:
        state.set_last_poll_iso(poll_started_at)
    # else: since_iso эскисича қолади — кейинги poll'да ХУДДИ ШУ ойна қайта текширилади

    # 4) Доставка стадиясига тушган сделкалар — БИР МАРТАЛИК хабар (кузатилмайди)
    try:
        delivery_errors = await _process_delivery_notifications(bot, since_iso, deal_state)
        errors.extend(delivery_errors)
    except Exception as e:
        log.exception("Доставка хабарларида хато: %s", e)
        errors.append(("—", f"Доставка хабарларида хато: {e}"))

    # 5) "В пути" стадиясига тушган сделкалар — Sheets'даги "Moy_sklad" устунини
    #    1'га белгилаймиз (Telegram хабари ЙЎҚ, фақат Sheets индикатори)
    try:
        await _process_moy_sklad_tracking(since_iso, deal_state)
    except Exception as e:
        log.exception("Moy_sklad кузатувида хато: %s", e)
        errors.append(("—", f"Moy_sklad кузатувида хато: {e}"))

    if errors:
        await _notify_admins_about_errors(bot, errors)


async def _process_delivery_notifications(bot, since_iso, deal_state):
    """Category=6 (Доставка) воронкасида, админ /adddeliverygroup билан
    бириктирган стадияларга тушган сделкаларга БИР МАРТА хабар юборади.
    Ҳеч қандай кузатиш/edit йўқ — фақат "тушди" деган фактга хабар.
    Sheets'даги "Pochta" устуни (агар сделка аввал confirm-оқимидан ўтган
    бўлса, deal_state'даги sheet_rows орқали) 1/0 билан янгиланади."""
    mapping = state.get_delivery_stage_groups()
    if not mapping:
        return []

    stage_ids = list(mapping.keys())
    try:
        deals = await _bx_call(bitrix.bx_get_deals_by_stages,
                                config.CATEGORY_DELIVERY, stage_ids, since_iso)
    except Exception as e:
        log.exception("Доставка сделкаларини олишда хато: %s", e)
        return [("—", f"Доставка сделкаларини олишда хато: {e}")]
    if deals is None:
        log.warning("Доставка сделкаларини олиб бўлмади (Bitrix уланиш хатоси).")
        return [("—", "Доставка сделкаларини олиб бўлмади (Bitrix уланиш хатоси)")]

    errors = []
    for deal in deals:
        deal_id = str(deal["ID"])
        stage_id = deal.get("STAGE_ID")
        chat_id = mapping.get(stage_id)
        if not chat_id:
            continue
        sheet_rows = deal_state.get(deal_id, {}).get("sheet_rows")
        try:
            stage_name = await _bx_call(bitrix.bx_get_stage_name,
                                         config.CATEGORY_DELIVERY, stage_id)
            region_id = str(deal.get(config.FIELD_REGION) or "")
            region_name = config.REGION_NAME_BY_ID.get(region_id, "")
            address = deal.get(config.FIELD_ADDRESS) or ""
            summa = deal.get("OPPORTUNITY") or 0

            client_name, phones, products_rows, source_name = await _fetch_deal_content(deal_id, deal)

            assigned_id = deal.get("ASSIGNED_BY_ID")
            bitrix_user = await _bx_call(bitrix.bx_get_user, assigned_id) if assigned_id else None
            operator_name = ((bitrix_user.get("NAME") or "") + " " +
                              (bitrix_user.get("LAST_NAME") or "")).strip() if bitrix_user else ""
            employee_number = bitrix.get_employee_number(bitrix_user) if bitrix_user else ""

            text = message_format.build_delivery_notification(
                deal_id=deal_id, stage_name=stage_name, products_rows=products_rows,
                summa=summa, region_name=region_name, address=address,
                client_name=client_name, phones=phones, operator_name=operator_name,
                employee_number=employee_number, source_name=source_name)

            await bot.send_message(chat_id=chat_id, text=text)
            log.info("Сделка %s: доставка хабари юборилди (стадия %s, канал %s).",
                      deal_id, stage_id, chat_id)
            if sheet_rows:
                await _bx_call(sheets.update_pochta_ok, sheet_rows, True)
        except Exception as e:
            log.exception("Сделка %s: доставка хабари юборилмади: %s", deal_id, e)
            errors.append((deal_id, str(e)))
            if sheet_rows:
                await _bx_call(sheets.update_pochta_ok, sheet_rows, False)


    return errors


async def _notify_admins_about_errors(bot, errors):
    """Poll давомида йиғилган хатоларни админларга БИТТА хабарда жўнатади
    (ҳар сделка учун алоҳида хабар эмас — 150+ сделка бўлганда спам бўлмаслиги учун)."""
    max_shown = 10
    lines = [f"⚠️ Poll'да {len(errors)} та хато (жараён давом этди, бошқа сделкалар ишланди):", ""]
    for deal_id, err in errors[:max_shown]:
        lines.append(f"• Сделка {deal_id}: {err[:150]}")
    if len(errors) > max_shown:
        lines.append(f"... яна {len(errors) - max_shown} та хато (тўлиқ рўйхат: bot.log)")
    text = "\n".join(lines)
    for aid in config.ADMIN_IDS:
        try:
            await bot.send_message(chat_id=aid, text=text)
        except Exception as e:
            log.error("Админга хато хабари юборилмади (%s): %s", aid, e)