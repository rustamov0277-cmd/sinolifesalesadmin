# -*- coding: utf-8 -*-
"""
SinolifeSalesAdmin v2 — конфигурация.
Барча "қаттиқ" қийматлар (Bitrix стадия кодлари, регион номлари ва ҳ.к.) шу ерда.
"""
import os

# ═══════════════════════ ENV (start.sh орқали келади) ═══════════════════════
TELEGRAM_TOKEN = os.environ.get("SA_TELEGRAM_TOKEN", "")
BITRIX_WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/") + "/"

ADMIN_IDS = set()
for _x in os.environ.get("SA_ADMIN_IDS", "").split(","):
    _x = _x.strip()
    if _x.isdigit():
        ADMIN_IDS.add(int(_x))

EMPLOYEE_EMAIL_DOMAIN = os.environ.get("SA_EMPLOYEE_EMAIL_DOMAIN", "sinolifemanager.uz")

# Ҳар неча сониядан бир марта Bitrix'ни текшириш (polling)
POLL_INTERVAL_SECONDS = int(os.environ.get("SA_POLL_INTERVAL", "45"))

# "Тасдиқланмади" сделкалар неча кун кузатилади (шундан кейин автомат
# тўхтатилади — акс ҳолда ойлар ўтгач минглаб эски сделка ҳар poll'да
# текширилиб, серверни секинлаштиради)
REJECTED_TRACK_DAYS = int(os.environ.get("SA_REJECTED_TRACK_DAYS", "3"))

# Бир поллда бир вақтнинг ўзида нечта сделкани параллел (thread'да)
# қайта ишлаш мумкин — Bitrix'ни "QUERY_LIMIT_EXCEEDED" билан урмаслик учун
MAX_CONCURRENT_BITRIX = int(os.environ.get("SA_MAX_CONCURRENT_BITRIX", "4"))

# "Кенгайтирилган аниқлаш" (poll оралиғида сакраб ўтган сделкаларни тутиб
# олиш) фақат ШУНЧА КУН ичида ЯРАТИЛГАН сделкаларга татбиқ этилади — акс
# ҳолда ойлар олдин рад этилган эски сделка бугун тасодифан таҳрирланса
# (изоҳ қўшилса ва ҳ.к.), бот уни хато равишда "янги воқеа" деб ўтказиб юборарди.
CATCHUP_MAX_AGE_DAYS = int(os.environ.get("SA_CATCHUP_MAX_AGE_DAYS", "3"))

# ═══════════════════════ Bitrix воронка / стадия кодлари ═══════════════════
# "Тасдиқлаш" воронкаси (category 4)
CATEGORY_CONFIRM = "4"
STAGE_CONFIRM_NEW = "C4:NEW"           # Заказ тасдиклаш      -> 🕔 Тасдиқлаш (БОШЛАНИШ НУҚТАСИ)
STAGE_CONFIRM_NODZVON = "C4:UC_JQR9F1"  # Недозвон смс         -> 🟡 Кутармади (нд)
STAGE_CONFIRM_LOSE = "C4:LOSE"          # Ошибка первичный отдел -> ❌ Тасдиқланмади

# Диққат: "Тасдиқлаш" воронкасида яна бошқа стадиялар ҳам бор
# (Смс zextra/коллаген тастиклаш, Пропущенный, UTECHKA, Сделка успешна) —
# улар ҳозирча статус хабарини ЎЗГАРТИРМАЙДИ (фақат 4 та асосий статус кузатилади).
CATEGORY_CONFIRM_IGNORED_STAGES = {
    "C4:PREPAYMENT_INVOICE",  # Смс zextra тастиклаш
    "C4:UC_GYMGQS",           # Смс коллаген тастиклаш
    "C4:FINAL_INVOICE",       # Пропущенный
    "C4:UC_V4JJIW",           # UTECHKA
    "C4:WON",                 # Сделка успешна (одатда автомат C6:NEW'га ўтади)
}

# "Доставка" воронкаси (category 6)
CATEGORY_DELIVERY = "6"
STAGE_DELIVERY_NEW = "C6:NEW"  # Подготовка товара -> ✅ Тасдиқланди

# "В пути" стадияси — Sheets'даги "Moy_sklad" устунини 1'га белгилаш учун
# (Telegram хабари эмас, фақат Sheets индикатори)
STAGE_DELIVERY_ON_THE_WAY = "C6:UC_4UD7I9"  # В пути

# МУҲИМ: "Ошибка первичный отдел" стадияси танланганда, Bitrix'нинг ўзи
# сделкани автомат равишда "Первичный отдел" (category 12) воронкасидаги
# "Тасдикланмаган" стадиясига кўчиради (C4:LOSE'да қолдирмайди!).
# Шунинг учун якуний "рад этилди" ҳолати шу ерда ушланади:
CATEGORY_PRIMARY = "12"
STAGE_PRIMARY_REJECTED = "C12:UC_1OM8B2"  # Тасдикланмаган

# "Янги/ўтказиб юборилган сделка"ни аниқлаш учун кузатиладиган ҳамма
# воронкалар — сделка ТЕЗ (poll оралиғидан тезроқ) бир нечта стадияни
# "сакраб ўтса" ҳам, шу рўйхатдаги воронкалардан бирида ЖОРИЙ турса —
# қайси стадияда бўлса ҳам аниқланади (фақат C4:NEW билан чекланмайди)
TRACKED_CATEGORIES = [CATEGORY_CONFIRM, CATEGORY_PRIMARY, CATEGORY_DELIVERY]

# Статус -> (эмодзи, матн) — каналдаги хабарнинг охирги қатори шундан қурилади
STATUS_LABELS = {
    "confirm_new":   ("🕔", "Тасдиқлаш"),
    "no_answer":     ("🟡", "Кутармади (нд)"),
    "rejected":      ("❌", "Тасдиқланмади"),
    "confirmed":     ("✅", "Тасдиқланди"),
}

# (CATEGORY_ID, STAGE_ID) -> status кaлит сўзи (юқоридаги STATUS_LABELS'га мос)
STAGE_TO_STATUS_KEY = {
    (CATEGORY_CONFIRM, STAGE_CONFIRM_NEW): "confirm_new",
    (CATEGORY_CONFIRM, STAGE_CONFIRM_NODZVON): "no_answer",
    (CATEGORY_CONFIRM, STAGE_CONFIRM_LOSE): "rejected",     # эҳтиёт учун (агар автоматика ишламаса)
    (CATEGORY_PRIMARY, STAGE_PRIMARY_REJECTED): "rejected",  # Bitrix автомат кўчиргандаги ҳақиқий жой
    (CATEGORY_DELIVERY, STAGE_DELIVERY_NEW): "confirmed",
}

# ═══════════════════════ Bitrix майдонлари (эски ботдан) ════════════════════
FIELD_REGION = "UF_CRM_1747975214161"
FIELD_ADDRESS = "UF_CRM_1748964117765"

REGIONS = [
    ("90", "Тошкент ш."), ("92", "Тошкент вил."), ("94", "Андижон"),
    ("96", "Бухоро"), ("98", "Жиззах"), ("100", "Қашқадарё"),
    ("102", "Навоий"), ("104", "Наманган"), ("106", "Самарқанд"),
    ("108", "Сурхондарё"), ("110", "Сирдарё"), ("112", "Фарғона"),
    ("114", "Хоразм"), ("116", "Нукус"),
]
REGION_NAME_BY_ID = {rid: name for rid, name in REGIONS}

# Telegram хабарни таҳрирлаш чегараси (Bitrix API'да эмас, Telegram'да):
# 48 соатдан кейин eski хабарни edit қилиб бўлмайди — шунда бот ЯНГИ хабар юборади.
TELEGRAM_EDIT_LIMIT_HOURS = 48

# ═══════════════════════ Google Sheets (ҳисобот учун) ═══════════════════════
SHEET_ID = os.environ.get("SA_SHEET_ID", "14-rGmriVBRFUlziFKSKdTVhpl8kK0GOke5Y4OpzYpmM")
SA_JSON = os.environ.get("SA_SA_JSON", "/root/sinolifesalesadmin_v2/service_account.json")
SHEET_WORKSHEET_NAME = os.environ.get("SA_SHEET_WORKSHEET", "Buyurtmalar_v2")