# -*- coding: utf-8 -*-
"""
Google Sheets'га ҳисобот учун ёзиш.

Устунлар тартиби (фойдаланувчи белгилаган):
  №, Sana, Vaqt, ROP, Operator, Mijoz, Telefon, Mahsulot, Soni, Summa,
  Region, Manzil, Deal_ID, Xodim_raqami, Status, Manba

Мантиқ:
  - Бир буюртмада N та маҳсулот бўлса — N та қатор қўшилади (ҳар маҳсулот
    алоҳида қаторда, сони алоҳида устунда).
  - Summa ФАҚАТ БИРИНЧИ қаторга ёзилади, қолган қаторларда бўш қолади
    (такрорланмаслиги учун).
  - Статус ўзгарса -> ШУ БУЮРТМАГА тегишли БАРЧА қаторларнинг "Status"
    устуни янгиланади (янги қатор қўшилмайди).
"""
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

import config

log = logging.getLogger("sheets")

HEADERS = ["№", "Sana", "Vaqt", "ROP", "Operator", "Mijoz", "Telefon",
           "Mahsulot", "Soni", "Summa", "Region", "Manzil", "Deal_ID",
           "Xodim_raqami", "Status", "Manba"]
STATUS_COL = HEADERS.index("Status") + 1  # gspread 1-индексли

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


def _combine_phones(phones):
    """Иккита телефонни битта устунга бирлаштиради: '+998... / +998...'."""
    phones = [p for p in (phones or []) if p]
    return " / ".join(phones[:2])


def log_new_order(order_num, deal_id, products_rows, summa, region_name,
                   address, client_name, phones, operator_name,
                   employee_number, status_key, rop_name="", source_name=""):
    """Ҳар маҳсулот учун алоҳида қатор қўшади (Summa фақат биринчисига).
    Қатор рақамлари рўйхатини қайтаради (кейинчалик статус update учун)."""
    try:
        ws = _ensure_ws()
        existing_rows = len(ws.get_all_values())  # header ҳам ҳисобга киради
        start_row = existing_rows + 1

        now = datetime.now()
        telefon = _combine_phones(phones)
        emoji, status_text = config.STATUS_LABELS[status_key]
        status_cell = status_text + " " + emoji
        clean_summa = _clean_money(summa)

        rows_to_add = []
        source_list = products_rows if products_rows else [{"PRODUCT_NAME": "—", "QUANTITY": ""}]

        for i, r in enumerate(source_list):
            name = r.get("PRODUCT_NAME") or "?"
            qty = _clean_qty(r.get("QUANTITY")) if r.get("QUANTITY") != "" else ""
            row_summa = clean_summa if i == 0 else ""  # фақат биринчи қаторга

            rows_to_add.append([
                order_num,                      # №
                now.strftime("%d.%m.%Y"),        # Sana
                now.strftime("%H:%M"),           # Vaqt
                rop_name,                        # ROP
                operator_name,                   # Operator
                client_name,                     # Mijoz
                telefon,                         # Telefon
                name,                            # Mahsulot
                qty,                              # Soni
                row_summa,                        # Summa
                region_name,                      # Region
                address,                          # Manzil
                deal_id,                          # Deal_ID
                employee_number,                  # Xodim_raqami
                status_cell,                      # Status
                source_name,                      # Manba
            ])

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