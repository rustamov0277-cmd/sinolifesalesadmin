# -*- coding: utf-8 -*-
"""
SinolifeSalesAdmin v2 — сделка статуси кузатувчиси.

Вазифа: Bitrix'даги "Тасдиқлаш" (кат.4) ва "Доставка" (кат.6) воронкаларини
кузатади, C4:NEW'га тушган сделка учун РОП каналига хабар юборади, стадия
ўзгарса ўша хабарни таҳрирлайди.

Буйруқлар (фақат админ):
  /listrops                  — "(ROP)" бўлимлари ва уларнинг Bitrix ID'си
  /addropgroup <rop_id> <chat_id> — РОПни каналга бириктириш
  /removeropgroup <rop_id>   — бириктиришни ўчириш
  /whoami                    — ўз Telegram/чат ID'ингизни кўриш (канал ID олиш учун қулай)
"""
import sys
import logging
import fcntl

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

import config
import state
import bitrix
import poller

# ── Бир нусхада ишлашни таъминлаш ───────────────────────────────────────────
_lock = open("/tmp/sinolifesalesadmin_v2.lock", "w")
try:
    fcntl.flock(_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.stderr.write("Бот аллақачон ишлаяпти.\n")
    sys.exit(1)

logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")


def is_admin(update):
    """Каналда (channel_post) юборилган буйруқда effective_user йўқ (анонимча
    ҳисобланади) — шунинг учун бу ерда None бўлиши мумкин, шунда False қайтади."""
    user = update.effective_user
    return bool(user) and user.id in config.ADMIN_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "👋 SinolifeSalesAdmin — сделка статуси кузатувчиси.\n"
        "Бу бот буюртма киритиш учун эмас, у Bitrix'даги сделка статусини "
        "автомат каналларга эълон қилади.\n\n"
        "Админ буйруқлари учун: /listrops")


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.effective_message.reply_text(
        "🆔 Chat ID: " + str(chat.id) + "\n"
        "📌 Тури: " + chat.type)


async def cmd_listrops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ Фақат админ (шахсий чатда ёзинг).")
        return
    depts = bitrix.bx_get_departments()
    rops = [d for d in depts if "(ROP)" in (d.get("NAME") or "")]
    if not rops:
        await update.effective_message.reply_text("РОП топилмади.")
        return
    rop_groups = state.load_rop_groups()
    lines = ["🏢 РОПлар рўйхати:", ""]
    for d in rops:
        rop_id = str(d.get("UF_HEAD", "?"))
        chat_id = rop_groups.get(rop_id)
        status = ("✅ канал: " + chat_id) if chat_id else "❌ канал бириктирилмаган"
        lines.append(d["NAME"] + " — Bitrix ID: " + rop_id + " (бўлим " +
                     str(d["ID"]) + ")\n   " + status)
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_addropgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ Фақат админ (шахсий чатда ёзинг).")
        return
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Қўллаш: /addropgroup РОП_Bitrix_ID КАНАЛ_ID\n"
            "(РОП Bitrix ID — /listrops билан кўринг.\n"
            "Канал ID — ботни каналга admin қилиб қўшиб, каналда /whoami ёзинг.)")
        return
    rop_id, chat_id = args[0], args[1]
    state.set_rop_group(rop_id, chat_id)
    await update.effective_message.reply_text("✅ Сақланди: РОП " + rop_id + " -> канал " + chat_id)


async def cmd_removeropgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.effective_message.reply_text("⛔ Фақат админ (шахсий чатда ёзинг).")
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Қўллаш: /removeropgroup РОП_Bitrix_ID")
        return
    rop_id = args[0]
    groups = state.load_rop_groups()
    if rop_id in groups:
        del groups[rop_id]
        state.save_rop_groups(groups)
        await update.effective_message.reply_text("✅ Ўчирилди: РОП " + rop_id)
    else:
        await update.effective_message.reply_text("⚠️ Бу РОП учун бириктирилган канал топилмади.")


# ── Polling job ──────────────────────────────────────────────────────────

async def job_poll(context: ContextTypes.DEFAULT_TYPE):
    try:
        await poller.poll_once(context.bot)
    except Exception as e:
        log.exception("poll_once хатоси: %s", e)
        for aid in config.ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=aid, text="⚠️ SinolifeSalesAdmin poll хатоси: " + str(e))
            except Exception:
                pass


if __name__ == "__main__":
    if not config.TELEGRAM_TOKEN:
        sys.exit("❌ SA_TELEGRAM_TOKEN o'rnatilmagan")
    if not config.BITRIX_WEBHOOK or config.BITRIX_WEBHOOK == "/":
        sys.exit("❌ BITRIX_WEBHOOK o'rnatilmagan")

    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("listrops", cmd_listrops))
    app.add_handler(CommandHandler("addropgroup", cmd_addropgroup))
    app.add_handler(CommandHandler("removeropgroup", cmd_removeropgroup))

    app.job_queue.run_repeating(job_poll, interval=config.POLL_INTERVAL_SECONDS, first=10)

    log.info("SinolifeSalesAdmin v2 ishga tushdi (poll ҳар %d сония).",
              config.POLL_INTERVAL_SECONDS)
    app.run_polling()