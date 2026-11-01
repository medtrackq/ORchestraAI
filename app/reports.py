"""Management reports: OR utilisation, first-case on-time starts, turnover,
accuracy of the booked duration, case mix, staff hours and supplies used.

``build_report`` works on plain records so it can be tested without a
database; the stores turn their bookings into ``CaseRecord`` / ``RoomInfo``.
Actual times are used when they were recorded, otherwise the booked times.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

DEFAULT_HOURS = (time(8, 0), time(16, 0))  # elective hours when a room has none set
ON_TIME_GRACE = 15  # minutes late still counted as an on-time first case
WITHIN = 15  # booked duration counted as accurate within +/- this many minutes
MAX_TURNOVER = 180  # longer gaps between cases are idle time, not turnover
# Actual minus booked minutes, in five bins (sign: + = took longer than booked).
ERROR_BINS = ["เร็วกว่าที่จอง 30+ นาที", "เร็วกว่า 15-30 นาที", "ตรง ±15 นาที", "นานกว่า 15-30 นาที",
              "นานกว่าที่จอง 30+ นาที"]


def error_bin(error: float) -> int:
    if error < -30:
        return 0
    if error < -WITHIN:
        return 1
    if error <= WITHIN:
        return 2
    return 3 if error <= 30 else 4


@dataclass
class CaseRecord:
    case_id: str
    room: str
    surgeon: str
    procedure: str
    urgency: str
    start: datetime
    end: datetime
    cleanup_end: datetime
    actual_start: datetime | None = None
    actual_end: datetime | None = None
    team: list[str] = field(default_factory=list)
    demo: bool = False  # invented demo history

    @property
    def booked_mins(self) -> int:
        return round((self.end - self.start).total_seconds() / 60)

    @property
    def actual_mins(self) -> int | None:
        if self.actual_start is None or self.actual_end is None or self.actual_end <= self.actual_start:
            return None
        return round((self.actual_end - self.actual_start).total_seconds() / 60)

    def span(self) -> tuple[datetime, datetime]:
        """In the room: actual times when recorded, else booked."""
        s = self.actual_start or self.start
        e = self.actual_end if self.actual_end and self.actual_end > s else self.end if self.end > s else s
        return s, e


@dataclass
class RoomInfo:
    code: str
    elective_start: time | None = None
    elective_end: time | None = None
    active: bool = True
    closures: list[tuple[datetime, datetime]] = field(default_factory=list)


def _overlap(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds() / 60)


def _pct(part: float, whole: float) -> float | None:
    return None if not whole else round(100 * part / whole, 1)


def _accuracy(rows: list[CaseRecord]) -> dict:
    errors = [r.actual_mins - r.booked_mins for r in rows if r.actual_mins is not None]
    if not errors:
        return {"n": 0, "mae": None, "bias": None, "within_pct": None, "overrun_pct": None}
    return {
        "n": len(errors),
        "mae": round(mean(abs(e) for e in errors), 1),
        "bias": round(mean(errors), 1),
        "within_pct": _pct(sum(abs(e) <= WITHIN for e in errors), len(errors)),
        "overrun_pct": _pct(sum(e > WITHIN for e in errors), len(errors)),
    }


def build_report(
    cases: list[CaseRecord],
    rooms: list[RoomInfo],
    first: date,
    last: date,
    tz: ZoneInfo,
    *,
    extra: dict | None = None,
) -> dict:
    """Everything the report page shows for ``first``..``last`` (inclusive)."""
    def local_day(ts: datetime) -> date:
        return ts.astimezone(tz).date()

    def at(day: date, clock: time) -> datetime:
        return datetime.combine(day, clock, tzinfo=tz)

    period = [c for c in cases if first <= local_day(c.start) <= last]
    by_room_day: dict[tuple[str, date], list[CaseRecord]] = defaultdict(list)
    for c in period:
        by_room_day[(c.room, local_day(c.start))].append(c)
    operating_days = sorted({d for _, d in by_room_day})

    # --- utilisation: in-room time (case + cleaning) inside elective hours / elective hours
    room_rows, day_used, day_avail = [], Counter(), Counter()
    total_avail = total_used = total_after = 0.0
    for room in rooms:
        if not room.active:
            continue
        avail = used = case_in = after = n = 0.0
        for day in operating_days:
            w0 = at(day, room.elective_start or DEFAULT_HOURS[0])
            w1 = at(day, room.elective_end or DEFAULT_HOURS[1])
            closed = sum(_overlap(a, b, w0, w1) for a, b in room.closures)
            window = max(0.0, (w1 - w0).total_seconds() / 60 - closed)
            avail += window
            day_avail[day] += window
            for c in by_room_day.get((room.code, day), []):
                s, e = c.span()
                turnover = max(timedelta(0), c.cleanup_end - c.end)
                inside = _overlap(s, e + turnover, w0, w1)
                used += inside
                day_used[day] += inside
                case_in += _overlap(s, e, w0, w1)
                after += (e - s).total_seconds() / 60 - _overlap(s, e, w0, w1)
                n += 1
        total_avail, total_used, total_after = total_avail + avail, total_used + used, total_after + after
        room_rows.append({
            "room": room.code, "cases": int(n), "available_hours": round(avail / 60, 1),
            "used_hours": round(used / 60, 1), "utilization": _pct(used, avail),
            "case_utilization": _pct(case_in, avail), "after_hours": round(after / 60, 1),
            "hours": f"{(room.elective_start or DEFAULT_HOURS[0]).strftime('%H:%M')}-"
                     f"{(room.elective_end or DEFAULT_HOURS[1]).strftime('%H:%M')}",
            "hours_set": room.elective_start is not None or room.elective_end is not None,
        })

    # --- first case of the day per room: on time when it starts within the grace
    first_cases = []
    for (room, day), rows in by_room_day.items():
        planned = sorted((c for c in rows if c.urgency != "EMERGENCY"), key=lambda c: c.start)
        if planned and planned[0].actual_start is not None:
            c = planned[0]
            first_cases.append((room, day, (c.actual_start - c.start).total_seconds() / 60))
    late = [d for _, _, d in first_cases]

    # --- turnover between consecutive cases in a room (both with actual times)
    turnovers, planned_turnovers = [], []
    for rows in by_room_day.values():
        timed = sorted((c for c in rows if c.actual_start and c.actual_end), key=lambda c: c.actual_start)
        for prev, nxt in zip(timed, timed[1:]):
            gap = (nxt.actual_start - prev.actual_end).total_seconds() / 60
            if 0 <= gap <= MAX_TURNOVER:
                turnovers.append(gap)
                planned_turnovers.append((prev.cleanup_end - prev.end).total_seconds() / 60)

    # --- booked duration vs actual
    timed = [c for c in period if c.actual_mins is not None]
    errors = [c.actual_mins - c.booked_mins for c in timed]
    counts = Counter(error_bin(e) for e in errors)
    bins = [{"label": label, "count": counts[i]} for i, label in enumerate(ERROR_BINS)]

    def groups(key) -> list[dict]:
        grouped: dict[str, list[CaseRecord]] = defaultdict(list)
        for c in timed:
            grouped[key(c)].append(c)
        rows = [{"name": name, **_accuracy(rows)} for name, rows in grouped.items()]
        return sorted(rows, key=lambda r: (-r["mae"], r["name"]))

    # --- case mix and staff hours
    mix = Counter(c.urgency for c in period)
    per_day = []
    for day in sorted({local_day(c.start) for c in period}):
        todays = [c for c in period if local_day(c.start) == day]
        per_day.append({
            "date": day.isoformat(),
            "elective": sum(c.urgency == "ELECTIVE" for c in todays),
            "unplanned": sum(c.urgency != "ELECTIVE" for c in todays),
            "utilization": _pct(day_used[day], day_avail[day]),
        })
    hours: Counter[str] = Counter()
    for c in period:
        s, e = c.span()
        for name in c.team:
            hours[name] += (e - s).total_seconds() / 3600

    return {
        "from": first.isoformat(), "to": last.isoformat(),
        "operating_days": len(operating_days),
        "cases": len(period),
        "cases_timed": len(timed),
        "demo_cases": sum(c.demo for c in period),
        "kpi": {
            "utilization": _pct(total_used, total_avail),
            "on_time_pct": _pct(sum(d <= ON_TIME_GRACE for d in late), len(late)),
            "first_cases_measured": len(late),
            "avg_first_case_delay": round(mean(max(0.0, d) for d in late), 1) if late else None,
            "turnover_avg": round(mean(turnovers), 1) if turnovers else None,
            "turnover_planned": round(mean(planned_turnovers), 1) if planned_turnovers else None,
            "turnovers_measured": len(turnovers),
            **{f"duration_{k}": v for k, v in _accuracy(period).items()},
            "after_hours": round(total_after / 60, 1),
        },
        "mix": {"ELECTIVE": mix["ELECTIVE"], "URGENT": mix["URGENT"], "EMERGENCY": mix["EMERGENCY"]},
        "per_day": per_day,
        "rooms": sorted(room_rows, key=lambda r: r["room"]),
        "error_bins": bins,
        "by_surgeon": groups(lambda c: c.surgeon),
        "by_procedure": groups(lambda c: c.procedure),
        "staff_hours": [{"name": n, "hours": round(h, 1)} for n, h in hours.most_common(12)],
        **(extra or {}),
    }


def previous_period(first: date, last: date) -> tuple[date, date]:
    days = (last - first).days + 1
    return first - timedelta(days=days), first - timedelta(days=1)
