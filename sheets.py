# -*- coding: utf-8 -*-
"""
Google Sheets'га ҳисобот учун ёзиш.

Мантиқ:
  - Бир буюртмада N та маҳсулот бўлса — N та қатор қўшилади (ҳар маҳсулот
    алоҳида қаторда, сони алоҳида устунда), лекин буюртма маълумотлари
    (сана, №, mijoz, telefon va h.k.) ҲАР БИР қаторда такрорланади.
  - Қатор рақамлари рўйхати (sheet_rows) deal_state'га сақланади.
  - Статус ўзгарса -> ШУ БУЮРТМАГА тегишли БАРЧА қаторларнинг "Status"
    устуни янгиланади (янги қатор қўшилмайди).
"""
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

import config

log = logging.getLogger("sheets")

HEADERS = ["Sana", "Vaqt", "№", "Deal_ID", "Mahsulot", "Soni", "Summa",
           "Region", "Manzil", "Mijoz", "Telefon1", "Telefon2", "Operator",
           "Xodim_raqami", "Status"]
STATUS_COL = len(HEADERS)  # охирги устун

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


def _clean_qty(qty):
    """QUANTITY'ни тоза бутун сонга айлантиради (масалан '2.000000' -> 2)."""
    try:
        return int(float(qty))
    except (TypeError, ValueError):
        return qty


def _clean_money(n):
    """Суммани тоза бутун сонга айлантиради (масалан 3500000.0 -> 3500000,
    Google Sheets'да '3500000.00000000' кўринишида чиқмаслиги учун)."""
    try:
        return int(round(float(n)))
    except (TypeError, ValueError):
        return n


def log_new_order(order_num, deal_id, products_rows, summa, region_name,
                   address, client_name, phones, operator_name,
                   employee_number, status_key):
    """Ҳар маҳсулот учун алоҳида қатор қўшади. Қатор рақамлари рўйхатини
    қайтаради (кейинчалик статус update учун)."""
    try:
        ws = _ensure_ws()
        existing_rows = len(ws.get_all_values())  # header ҳам ҳисобга киради
        start_row = existing_rows + 1

        now = datetime.now()
        phone1 = phones[0] if len(phones) > 0 else ""
        phone2 = phones[1] if len(phones) > 1 else ""
        emoji, status_text = config.STATUS_LABELS[status_key]
        status_cell = status_text + " " + emoji
        clean_summa = _clean_money(summa)

        common = [now.strftime("%d.%m.%Y"), now.strftime("%H:%M"),
                  order_num, deal_id]  # Sana, Vaqt, №, Deal_ID
        tail = [clean_summa, region_name, address, client_name, phone1, phone2,
                operator_name, employee_number, status_cell]

        rows_to_add = []
        if products_rows:
            for r in products_rows:
                name = r.get("PRODUCT_NAME") or "?"
                qty = _clean_qty(r.get("QUANTITY"))
                rows_to_add.append(common + [name, qty] + tail)
        else:
            rows_to_add.append(common + ["—", ""] + tail)

        ws.append_rows(rows_to_add, value_input_option="USER_ENTERED")

        row_numbers = list(range(start_row, start_row + len(rows_to_add)))
        return row_numbers
    except Exception as e:
        log.error("log_new_order: %s", e)
        return []


def update_status(row_numbers, status_key):
    if not row_numbers:
        return
    try:
        ws = _ensure_ws()
        emoji, status_text = config.STATUS_LABELS[status_key]
        status_cell = status_text + " " + emoji
        for row_number in row_numbers:
            ws.update_cell(row_number, STATUS_COL, status_cell)
    except Exception as e:
        log.error("update_status(rows=%s): %s", row_numbers, e)