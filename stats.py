# -*- coding: utf-8 -*-
"""
Кунлик статистика — шу кун C4:NEW'га тушган буюртмалар бўйича умумий ва
ҳар РОП бўйича ажратма ҳисоблайди.

Ҳисоблаш манбаси: deal_state.json — ҳар ёзувнинг "sent_at" (буюртма биринчи
марта юборилган вақт) шу кунга тўғри келса, ўша ёзув ҳисобга киради.
Статус — ЖОРИЙ ҳолат (status_key), яъни кун давомида статус бир неча марта
ўзгарган бўлса ҳам, охирги ҳолати олинади.
"""
from datetime import datetime

import state

STATUS_KEYS = ["confirm_new", "no_answer", "confirmed", "rejected"]
EMPTY_COUNTS = {k: 0 for k in STATUS_KEYS}


def _empty_counts():
    return dict(EMPTY_COUNTS)


def compute_daily_stats(target_date):
    """target_date — datetime.date (Тошкент). Қайтаради: (total, counts, rop_counts)."""
    data = state.load_deal_state()
    total = 0
    counts = _empty_counts()
    rop_counts = {}

    for entry in data.values():
        sent_at = entry.get("sent_at")
        if not sent_at:
            continue
        try:
            dt = datetime.fromisoformat(sent_at)
        except ValueError:
            continue
        if dt.astimezone(state.TZ).date() != target_date:
            continue

        total += 1
        status_key = entry.get("status_key", "confirm_new")
        if status_key not in counts:
            continue
        counts[status_key] += 1

        rop_name = entry.get("rop_name") or "Номаълум"
        rop_counts.setdefault(rop_name, _empty_counts())
        rop_counts[rop_name][status_key] += 1

    return total, counts, rop_counts


def build_daily_stats_text(target_date=None):
    if target_date is None:
        target_date = state.now_tz().date()

    total, counts, rop_counts = compute_daily_stats(target_date)

    lines = [
        f"📊 Кунлик статистика — {target_date.strftime('%d.%m.%Y')}",
        "",
        f"Жами буюртма: {total}",
        f"✅ Тасдиқланди: {counts['confirmed']}",
        f"🟡 Кутармади (нд): {counts['no_answer']}",
        f"❌ Тасдиқланмади: {counts['rejected']}",
        f"🕔 Кутилмоқда (жавоб йўқ): {counts['confirm_new']}",
    ]

    if rop_counts:
        lines.append("")
        lines.append("👥 РОПлар бўйича:")
        for rop_name in sorted(rop_counts.keys()):
            c = rop_counts[rop_name]
            rop_total = sum(c.values())
            lines.append(
                f"• {rop_name}: жами {rop_total} — "
                f"✅{c['confirmed']} 🟡{c['no_answer']} ❌{c['rejected']} 🕔{c['confirm_new']}"
            )

    return "\n".join(lines)