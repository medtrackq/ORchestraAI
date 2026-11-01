"""Time helpers.

The engine works in integer minutes from the start of the planning horizon
(minute 0 = 00:00 of the first planning day).
"""
from __future__ import annotations

MINUTES_PER_DAY = 24 * 60


def hm(text: str, day: int = 0) -> int:
    """Convert "HH:MM" (optionally on a later day) to horizon minutes."""
    hours, minutes = text.split(":")
    return day * MINUTES_PER_DAY + int(hours) * 60 + int(minutes)


def fmt(minute: int) -> str:
    """Format horizon minutes as "HH:MM", naming the day when it is not day 0 (Thai)."""
    day, rem = divmod(int(minute), MINUTES_PER_DAY)
    text = f"{rem // 60:02d}:{rem % 60:02d}"
    if day == 0:
        return text
    return f"{text} " + {1: "(พรุ่งนี้)", -1: "(เมื่อวาน)"}.get(day, f"({day:+d} วัน)")
