#!/bin/bash
# /root/sinolifesalesadmin_v2/start.sh
# ДИҚҚАТ: бу файлда сирлар (токенлар) бор — фақат серверда, hech qayerga юбормаслик.

set -euo pipefail
cd "$(dirname "$0")"

# ── Telegram ──────────────────────────────────────────────────────────────
export SA_TELEGRAM_TOKEN="ЭСКИ_SINOLIFESALESADMIN_BOT_TOKENI_BU_YERGA"
export SA_ADMIN_IDS="TELEGRAM_ID_1,TELEGRAM_ID_2"   # админларнинг Telegram ID'лари, вергул билан

# ── Bitrix (ЯНГИ, алоҳида яратилган Входящий вебхук) ────────────────────
export BITRIX_WEBHOOK="https://obey.bitrix24.kz/rest/<user_id>/<yangi_webhook_kod>/"

# ── Ходим email домени (эски билан бир хил) ─────────────────────────────
export SA_EMPLOYEE_EMAIL_DOMAIN="sinolifemanager.uz"

# ── Poll оралиғи (сония) ─────────────────────────────────────────────────
export SA_POLL_INTERVAL="90"

# ── Google Sheets ─────────────────────────────────────────────────────────
export SA_SHEET_ID="14-rGmriVBRFUlziFKSKdTVhpl8kK0GOke5Y4OpzYpmM"
export SA_SA_JSON="/root/sinolifesalesadmin_v2/service_account.json"
export SA_SHEET_WORKSHEET="Buyurtmalar_v2"

# ── Ҳолат файллари қаерда сақлансин ──────────────────────────────────────
export SA_STATE_DIR="/root/sinolifesalesadmin_v2/state"

exec python3 bot.py
