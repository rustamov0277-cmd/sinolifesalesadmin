# -*- coding: utf-8 -*-
"""
Google Sheets'га ҳисобот учун ёзиш.

Мантиқ:
  - Янги буюртма -> янги қатор қўшилади, қатор рақами deal_state'га
    "sheet_row" сифатида сақланади.
  - Статус ўзгарса -> ўша қаторнинг "Status" устуни ЯНГИЛАНАДИ (янги қатор эмас).
"""
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

import config
import message_format

log = logging.getLogger("sheets")

HEADERS = ["Sana", "Vaqt", "№", "Deal_ID", "Mahsulotlar", "Summa", "Region",
           "Manzil", "Mijoz", "Telefon1", "Telefon2", "Operator",
           "Xodim_raqami", "Status"]
STATUS_COL = len(HEADERS)  # охирги устун (14 -> N)

_book_cache = {"book": None}


def _book():
    if _book_cache["book"] is not None:
        return _book_cache["book"]
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(config.SA_JSON, scopes=scopes)
    book = gspread.authorize(creds).open_by_key(config.SHEET_ID)
    _book_cache["book"] = book
    return book


def _ensure_ws():
    book = _book()
    try:
        return book.worksheet(config.SHEET_WORKSHEET_NAME)
    except Exception:
        ws = book.add_worksheet(title=config.SHEET_WORKSHEET_NAME, rows=5000,
                                 cols=len(HEADERS) + 2)
        ws.append_row(HEADERS)
        return ws


def log_new_order(order_num, deal_id, products_rows, summa, region_name,
                   address, client_name, phones, operator_name,
                   employee_number, status_key):
    """Янги қатор қўшади, қатор рақамини қайтаради (кейинчалик update учун)."""
    try:
        ws = _ensure_ws()
        existing_rows = len(ws.get_all_values())  # header ҳам ҳисобга киради
        row_number = existing_rows + 1

        now = datetime.now()
        phone1 = phones[0] if len(phones) > 0 else ""
        phone2 = phones[1] if len(phones) > 1 else ""
        emoji, status_text = config.STATUS_LABELS[status_key]

        ws.append_row([
            now.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
            order_num, deal_id, message_format.format_products(products_rows),
            summa, region_name, address, client_name, phone1, phone2,
            operator_name, employee_number, status_text + " " + emoji,
        ])
        return row_number
    except Exception as e:
        log.error("log_new_order: %s", e)
        return None


def update_status(row_number, status_key):
    if not row_number:
        return
    try:
        ws = _ensure_ws()
        emoji, status_text = config.STATUS_LABELS[status_key]
        ws.update_cell(row_number, STATUS_COL, status_text + " " + emoji)
    except Exception as e:
        log.error("update_status(row=%s): %s", row_number, e)
