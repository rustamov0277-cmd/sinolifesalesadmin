# -*- coding: utf-8 -*-
"""Каналдаги буюртма хабарининг матнини қуради (яратиш ва таҳрирлаш учун бир хил формат)."""
import config


def format_products(rows):
    """crm.deal.productrows.get натижасидан 'Ном - N ta' қаторлари."""
    lines = []
    for r in rows:
        name = r.get("PRODUCT_NAME") or "?"
        qty = r.get("QUANTITY")
        try:
            qty_str = str(int(float(qty)))
        except (TypeError, ValueError):
            qty_str = str(qty)
        lines.append(f"{name} - {qty_str} ta")
    return "\n".join(lines) if lines else "—"


def format_money(n):
    try:
        return f"{float(n):,.0f}".replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


def format_status_line(status_key):
    emoji, text = config.STATUS_LABELS[status_key]
    return f"{text} {emoji}"


def build_order_message(order_num, deal_id, products_rows, summa, region_name,
                         address, client_name, phones, operator_name,
                         employee_number, status_key, source_name=""):
    """
    Тўлиқ хабар матни:

    №006
    🗒Id сделки: 363678
    🌐Источник: ...        (бор бўлса)
    📦Продукт: ...
    💵Сумма: 2350000
    📍Регион: Андижан
    🚚Адрес: ...
    👤Имя клиента: ...
    📞Телефон: +998...
    📞Телефон: +998...   (иккинчиси бор бўлсагина)
    Оператор: Исм Фамилия 119

    Тастиклаш 🕔
    """
    lines = [
        f"№{order_num:03d}",
        f"🗒Id сделки: {deal_id}",
        f"📦Продукт: {format_products(products_rows)}",
        f"💵Сумма: {format_money(summa)}",
        f"📍Регион: {region_name or '—'}",
        f"🚚Адрес: {address or '—'}",
        f"👤Имя клиента: {client_name or '—'}",
    ]
    for phone in phones[:2]:  # фақат мавжуд бўлса — 1 ёки 2 та қатор
        lines.append(f"📞Телефон: {phone}")

    operator_line = "Оператор: " + (operator_name or "—")
    if employee_number:
        operator_line += f" {employee_number}"
    lines.append(operator_line)

    if source_name:
        lines.append(f"🌐Источник: {source_name}")

    lines.append("")  # бўш қатор
    lines.append(format_status_line(status_key))

    return "\n".join(lines)


def replace_status_line(old_text, new_status_key):
    """Мавжуд хабар матнида фақат охирги (статус) қаторини алмаштиради."""
    lines = old_text.split("\n")
    # Охирги нобўш қаторни статус деб оламиз ва алмаштирамиз
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            lines[i] = format_status_line(new_status_key)
            return "\n".join(lines)
    # агар матн бўш бўлса (кутилмаган ҳолат) — шунчаки қўшиб қўямиз
    lines.append(format_status_line(new_status_key))
    return "\n".join(lines)