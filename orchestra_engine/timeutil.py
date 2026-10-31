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
    """Format horizon minutes as "HH:MM", with a "+Nd" suffix past day 0."""
    day, rem = divmod(int(minute), MINUTES_PER_DAY)
    text = f"{rem // 60:02d}:{rem % 60:02d}"
    return text if day == 0 else f"{text}(+{day}d)"
