"""Staffing Agent fatigue rules, shared by the solver, the validator and the UI.

Three limits per person (``StaffMember``):

* weekly: operating-room time over the last 7 days (``current_weekly_hours``
  before the horizon + cases in the horizon) <= ``max_weekly_hours``
* daily: operating-room time inside the planning day <= ``max_daily_hours``
* rest: after a case that ends late at night (after 22:00 or by 06:00) the
  person rests ``rest_hours`` before the next case; ``rest_until`` carries a
  late case from the day before, ``next_booking_min`` the first booking after
  the planning day.

Normal cases must keep every limit. EMERGENCY cases may break them (hospital
policy "A"); each break is reported as an issue with ``override=True`` so the
UI can show who is over and by how much. Issues on cases that already started
(``locked``) are history: they are reported but cannot be changed.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Mapping

from .models import StaffMember, SurgeryCase, Urgency
from .results import Assignment
from .timeutil import MINUTES_PER_DAY, fmt

NIGHT_START = 22 * 60  # a case ending after 22:00 ...
NIGHT_END = 6 * 60  # ... or by 06:00 counts as night work


def is_late_end(end_min: int) -> bool:
    """Does a case ending at ``end_min`` (horizon minutes) end at night?"""
    t = end_min % MINUTES_PER_DAY
    return t > NIGHT_START or t <= NIGHT_END


def late_windows(lo: int, hi: int) -> list[tuple[int, int]]:
    """Night-end windows ``[a, b]`` (inclusive) intersecting ``[lo, hi]``."""
    out = []
    day = lo // MINUTES_PER_DAY - 1
    while day * MINUTES_PER_DAY + NIGHT_START < hi:
        a = day * MINUTES_PER_DAY + NIGHT_START + 1
        b = (day + 1) * MINUTES_PER_DAY + NIGHT_END
        if b >= lo and a <= hi:
            out.append((max(a, lo), min(b, hi)))
        day += 1
    return out


def day_minutes(start: int, end: int, horizon: int = MINUTES_PER_DAY) -> int:
    """Minutes of ``[start, end)`` inside the planning day ``[0, horizon)``."""
    return max(0, min(end, horizon) - max(start, 0))


@dataclass(frozen=True)
class FatigueIssue:
    staff_id: str
    name: str
    case_id: str
    kind: str  # weekly / daily / rest / next_day
    minutes: int  # over the limit by / rest short by
    limit_mins: int  # the limit (cap, or rest length)
    actual_mins: int  # load reached, or rest actually taken
    override: bool  # allowed because the case is an EMERGENCY
    locked: bool  # the case had already started
    after_case: str | None = None  # rest: the late case before (None = another day)
    until: int | None = None  # rest: when rest ends; next_day: next booking start

    @property
    def is_error(self) -> bool:
        """A break the scheduler must never create."""
        return not self.override and not self.locked

    @property
    def key(self) -> tuple:
        return (self.staff_id, self.case_id, self.kind)

    def detail(self) -> str:
        h = lambda m: f"{m / 60:.1f} h"  # noqa: E731
        if self.kind == "weekly":
            text = f"{self.name}: {h(self.actual_mins)} in 7 days > {h(self.limit_mins)} (+{self.minutes} min)"
        elif self.kind == "daily":
            text = f"{self.name}: {h(self.actual_mins)} today > {h(self.limit_mins)} (+{self.minutes} min)"
        elif self.kind == "rest":
            after = self.after_case or "a late case on the previous day"
            text = (f"{self.name}: {self.case_id} starts {self.minutes} min before the {h(self.limit_mins)} rest "
                    f"after {after} ends ({fmt(self.until)})")
        else:
            text = (f"{self.name}: {self.case_id} ends at night, only {h(self.actual_mins)} rest before the next "
                    f"booking at {fmt(self.until)} (needs {h(self.limit_mins)})")
        return text + (" [EMERGENCY override]" if self.override else "") + (" [already started]" if self.locked else "")

    def to_dict(self) -> dict:
        return {
            "staff_id": self.staff_id, "name": self.name, "case_id": self.case_id, "kind": self.kind,
            "minutes": self.minutes, "limit_mins": self.limit_mins, "actual_mins": self.actual_mins,
            "override": self.override, "locked": self.locked, "after_case": self.after_case,
            "until": self.until, "detail": self.detail(),
        }


def fatigue_issues(
    assignments: Mapping[str, Assignment],
    cases: Mapping[str, SurgeryCase] | Iterable[SurgeryCase],
    staff: Iterable[StaffMember],
    *,
    horizon_mins: int = MINUTES_PER_DAY,
) -> list[FatigueIssue]:
    """Every fatigue-limit break in a schedule (see the module docstring)."""
    case_by_id = cases if isinstance(cases, Mapping) else {c.case_id: c for c in cases}
    staff_by_id = {s.staff_id: s for s in staff}
    mine: dict[str, list[Assignment]] = defaultdict(list)
    for a in assignments.values():
        if a.case_id in case_by_id:
            for sid in a.staff_ids:
                if sid in staff_by_id:
                    mine[sid].append(a)

    issues: list[FatigueIssue] = []
    for sid, items in mine.items():
        p = staff_by_id[sid]
        items.sort(key=lambda a: (a.start_min, a.case_id))
        emergency = lambda a: case_by_id[a.case_id].urgency is Urgency.EMERGENCY  # noqa: E731

        def cap_issue(kind: str, cap: int, prior: int, load) -> None:
            locked = prior + sum(load(a) for a in items if a.frozen)
            free_e = sum(load(a) for a in items if not a.frozen and emergency(a))
            free_n = sum(load(a) for a in items if not a.frozen and not emergency(a))
            total = locked + free_e + free_n
            if total <= cap:
                return
            room = max(0, cap - locked)
            over_n = max(0, free_n - room)  # normal cases beyond what is left
            last = items[-1]
            for a in reversed(items):  # blame the latest case that pushed it over
                if not a.frozen and (over_n == 0 or not emergency(a)):
                    last = a
                    break
            issues.append(FatigueIssue(
                sid, p.name, last.case_id, kind, total - cap, cap, total,
                override=over_n == 0 and free_e > 0, locked=free_e + free_n == 0,
            ))

        cap_issue("weekly", p.weekly_cap_minutes, p.weekly_used_minutes, lambda a: a.end_min - a.start_min)
        cap_issue("daily", p.daily_cap_minutes, 0, lambda a: day_minutes(a.start_min, a.end_min, horizon_mins))

        rest = p.rest_minutes
        for j in items:  # a case starting inside someone's rest
            reasons = []
            if j.start_min < p.rest_until:
                reasons.append((None, p.rest_until))
            for i in items:
                if i is not j and is_late_end(i.end_min) and i.end_min <= j.start_min < i.end_min + rest:
                    reasons.append((i.case_id, i.end_min + rest))
            if reasons:
                after, until = max(reasons, key=lambda r: r[1])
                issues.append(FatigueIssue(
                    sid, p.name, j.case_id, "rest", until - j.start_min, rest, rest - (until - j.start_min),
                    override=emergency(j), locked=j.frozen, after_case=after, until=until,
                ))
        nxt = p.next_booking_min
        if nxt is not None:
            for i in items:
                if is_late_end(i.end_min) and i.end_min + rest > nxt:
                    issues.append(FatigueIssue(
                        sid, p.name, i.case_id, "next_day", i.end_min + rest - nxt, rest, max(0, nxt - i.end_min),
                        override=emergency(i), locked=i.frozen, until=nxt,
                    ))
    return issues


def staff_fatigue(
    staff: Iterable[StaffMember],
    assignments: Mapping[str, Assignment],
    issues: Iterable[FatigueIssue] = (),
    *,
    now_min: int = 0,
    horizon_mins: int = MINUTES_PER_DAY,
) -> list[dict]:
    """Per-person load, limits and a 0-100 fatigue score for the Staffing view.

    score = the higher of (7-day OR time / weekly limit) and (today's OR time /
    daily limit), in percent; over 100 means a limit was broken (emergency).
    """
    by_person: dict[str, list[Assignment]] = defaultdict(list)
    for a in assignments.values():
        for sid in a.staff_ids:
            by_person[sid].append(a)
    issue_map: dict[str, list[dict]] = defaultdict(list)
    for i in issues:
        issue_map[i.staff_id].append(i.to_dict())
    out = []
    for p in staff:
        mine = sorted(by_person.get(p.staff_id, []), key=lambda a: a.start_min)
        today = sum(day_minutes(a.start_min, a.end_min, horizon_mins) for a in mine)
        week = p.weekly_used_minutes + sum(a.end_min - a.start_min for a in mine)
        score = round(100 * max(week / max(p.weekly_cap_minutes, 1), today / max(p.daily_cap_minutes, 1)))
        rest_until = p.rest_until if p.rest_until > now_min else None
        for a in mine:
            if is_late_end(a.end_min) and a.end_min + p.rest_minutes > now_min and a.end_min <= horizon_mins:
                rest_until = max(rest_until or 0, a.end_min + p.rest_minutes)
        level = "over" if score > 100 else "high" if score >= 90 else "tired" if score >= 70 else "ok"
        out.append({
            "staff_id": p.staff_id, "name": p.name, "role": p.role, "skills": sorted(p.skills),
            "active": p.is_active,
            "shifts": [f"{fmt(a)}-{fmt(b)}" for a, b in p.shifts],
            "cases": [a.case_id for a in mine],
            "today_mins": today, "daily_cap_mins": p.daily_cap_minutes,
            "week_mins": week, "weekly_cap_mins": p.weekly_cap_minutes,
            "rest_until": rest_until, "rest_mins": p.rest_minutes,
            "score": score, "level": level,
            "issues": issue_map.get(p.staff_id, []),
        })
    return out


def load_percent(person: StaffMember, frozen_minutes: int, frozen_day_minutes: int) -> int:
    """Fatigue score before a solve (what is already fixed), used to spread work."""
    week = (person.weekly_used_minutes + frozen_minutes) / max(person.weekly_cap_minutes, 1)
    day = frozen_day_minutes / max(person.daily_cap_minutes, 1)
    return min(100, round(100 * max(week, day)))
