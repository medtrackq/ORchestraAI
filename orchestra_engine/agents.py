"""Resource agents' point-in-time scan.

Before the solver runs, the Master Orchestrator asks the three resource
agents (Staffing, Equipment & CSSD, OR Facility) whether a case could start
at a requested time given what is already booked. Each agent answers with a
verdict, one line per resource it looked at, and the earliest time its own
resources would be ready, which is a hint for the negotiation step.
These checks are exact for a single new case; the solver then confirms or
finds the next slot that satisfies all agents at once.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping

from .models import SurgeryCase
from .results import Assignment, ScheduleResult
from .timeutil import fmt

if TYPE_CHECKING:
    from .optimizer import ORchestraOptimizer

STAFFING = "Staffing Agent"
EQUIPMENT = "Equipment & CSSD Agent"
FACILITY = "OR Facility Agent"

_FAR = 10**9
_STATUS_WORDS = {"STERILIZING": "sterilizing at CSSD", "IN_USE": "in use", "MAINTENANCE": "in maintenance"}

Span = tuple[int, int, str]  # (start, end, case_id)


@dataclass
class ResourceStatus:
    resource: str
    ok: bool
    note: str
    ready_at: int | None = None  # earliest start this resource could support


@dataclass
class AgentVerdict:
    agent: str
    ok: bool
    items: list[ResourceStatus]
    ready_at: int | None  # earliest start the agent could support; None = not in horizon

    def summary(self) -> str:
        return "; ".join(f"{i.resource} {i.note}" for i in self.items)


@dataclass
class SlotCheck:
    """Outcome of asking for a case at a given time."""

    case: SurgeryCase
    requested_start: int
    verdicts: list[AgentVerdict]
    result: ScheduleResult  # current bookings + the case where it was placed
    log: list[str]

    @property
    def ok(self) -> bool:
        return all(v.ok for v in self.verdicts)

    @property
    def assignment(self) -> Assignment | None:
        return self.result.assignments.get(self.case.case_id)

    def report(self) -> str:
        return "\n".join(self.log)

    def to_dict(self) -> dict:
        return {
            "case_id": self.case.case_id,
            "requested_start_min": self.requested_start,
            "requested_start": fmt(self.requested_start),
            "ok": self.ok,
            "verdicts": [
                {
                    "agent": v.agent,
                    "ok": v.ok,
                    "ready_at_min": v.ready_at,
                    "items": [
                        {"resource": i.resource, "ok": i.ok, "note": i.note, "ready_at_min": i.ready_at}
                        for i in v.items
                    ],
                }
                for v in self.verdicts
            ],
            "log": self.log,
            "result": self.result.to_dict(),
        }


@dataclass
class Busy:
    """Booked time per resource, taken from existing assignments."""

    room: dict[str, list[Span]]
    staff: dict[str, list[Span]]
    unit: dict[str, list[Span]]
    bed: dict[str, list[Span]]
    load: dict[str, int]  # booked minutes per person

    @classmethod
    def from_assignments(cls, assignments: Mapping[str, Assignment]) -> "Busy":
        busy = cls({}, {}, {}, {}, {})
        for a in assignments.values():
            busy.room.setdefault(a.or_id, []).append((a.start_min, a.cleanup_end_min, a.case_id))
            for sid in a.staff_ids:
                busy.staff.setdefault(sid, []).append((a.start_min, a.end_min, a.case_id))
                busy.load[sid] = busy.load.get(sid, 0) + (a.end_min - a.start_min)
            for uid, ready in a.equipment_ready_min:
                busy.unit.setdefault(uid, []).append((a.start_min, ready, a.case_id))
            if a.bed_id is not None:
                busy.bed.setdefault(a.bed_id, []).append(
                    (a.recovery_start_min, a.recovery_end_min, a.case_id)
                )
        return busy


def earliest_free(
    spans: list[Span], occupy: int, t0: int, windows: list[tuple[int, int]], fit: int, latest: int
) -> int | None:
    """Earliest t >= t0 where [t, t+occupy) misses every span and [t, t+fit)
    lies inside one window. Candidates are t0, window starts and span ends."""
    candidates = {t0} | {e for _, e, _ in spans if e > t0} | {a for a, _ in windows if a > t0}
    for t in sorted(candidates):
        if t > latest:
            return None
        if not any(a <= t and t + fit <= b for a, b in windows):
            continue
        if any(s < t + occupy and t < e for s, e, _ in spans):
            continue
        return t
    return None


def _clash(spans: list[Span], start: int, end: int) -> Span | None:
    return next((sp for sp in sorted(spans) if sp[0] < end and start < sp[1]), None)


def _clash_note(span: Span, start: int, ongoing: str, upcoming: str) -> str:
    s, e, cid = span
    if s <= start:
        return ongoing.format(case=cid, end=fmt(e))
    return upcoming.format(case=cid, start=fmt(s))


def _kth_ready(states: list[ResourceStatus], k: int) -> int | None:
    times = sorted(s.ready_at for s in states if s.ready_at is not None)
    return times[k - 1] if len(times) >= k else None


def _combine(readies: list[int | None]) -> int | None:
    return None if any(r is None for r in readies) else max(readies, default=None)


def scan_slot(
    opt: "ORchestraOptimizer", case: SurgeryCase, start: int, busy: Busy
) -> list[AgentVerdict]:
    """Ask the three resource agents about ``case`` starting at ``start``."""
    return [
        _staffing(opt, case, start, busy),
        _equipment(opt, case, start, busy),
        _facility(opt, case, start, busy),
    ]


def _staffing(opt, case: SurgeryCase, start: int, busy: Busy) -> AgentVerdict:
    dur = case.duration_mins
    end = start + dur
    latest = opt.horizon_mins - dur

    def person(p) -> ResourceStatus:
        left = p.remaining_minutes - busy.load.get(p.staff_id, 0)
        if not p.is_active:
            return ResourceStatus(p.name, False, "inactive")
        if left < dur:
            return ResourceStatus(p.name, False, f"weekly-hour cap reached ({max(left, 0)} min left)")
        spans = busy.staff.get(p.staff_id, [])
        nxt = earliest_free(spans, dur, start, list(p.shifts), dur, latest)
        clash = _clash(spans, start, end)
        if clash:
            note = _clash_note(clash, start, "busy with {case} until {end}", "booked for {case} at {start}")
            return ResourceStatus(p.name, False, note, nxt)
        if not any(a <= start and end <= b for a, b in p.shifts):
            shifts = ", ".join(f"{fmt(a)}-{fmt(b)}" for a, b in p.shifts) or "none"
            return ResourceStatus(p.name, False, f"off shift (shifts {shifts})", nxt)
        return ResourceStatus(p.name, True, f"free {fmt(start)}-{fmt(end)}", start)

    items: list[ResourceStatus] = []
    readies: list[int | None] = []
    surgeon = opt._staff_by_id.get(case.surgeon_id)
    if surgeon is None:
        items.append(ResourceStatus(case.surgeon_id, False, "unknown surgeon"))
        readies.append(None)
    else:
        status = person(surgeon)
        items.append(status)
        readies.append(status.ready_at)

    for role, count in case.required_staff_roles.items():
        if not count:
            continue
        skills = case.required_staff_skills.get(role, frozenset())
        pool = [
            p for p in opt.staff
            if p.role == role and p.staff_id != case.surgeon_id and skills <= p.skills
        ]
        states = [person(p) for p in pool]
        free = [s for s in states if s.ok]
        label = role.replace("_", " ").title() + (f" [{', '.join(sorted(skills))}]" if skills else "")
        if len(free) >= count:
            names = ", ".join(s.resource for s in free[:3]) + ("..." if len(free) > 3 else "")
            items.append(ResourceStatus(label, True, f"{len(free)} free ({names})", start))
            readies.append(start)
        else:
            ready = _kth_ready(states, count)
            when = f"enough from {fmt(ready)}" if ready is not None else "not enough today"
            items.append(ResourceStatus(label, False, f"{len(free)}/{count} free, {when}", ready))
            readies.append(ready)
    return AgentVerdict(STAFFING, all(i.ok for i in items), items, _combine(readies))


def _equipment(opt, case: SurgeryCase, start: int, busy: Busy) -> AgentVerdict:
    dur = case.duration_mins
    latest = opt.horizon_mins - dur
    items: list[ResourceStatus] = []
    readies: list[int | None] = []
    ok = True
    for eq_type, count in sorted(case.equipment_counts.items()):
        units = [u for u in opt.equipments if u.eq_type == eq_type]
        if not units:
            items.append(ResourceStatus(eq_type, False, "no unit of this type"))
            readies.append(None)
            ok = False
            continue
        states = []
        for u in units:
            label = f"{u.unit_id} ({eq_type})"
            if not u.is_usable:
                states.append(ResourceStatus(label, False, _STATUS_WORDS.get(u.status, u.status.lower())))
                continue
            occupy = dur + u.reprocess_mins
            spans = busy.unit.get(u.unit_id, [])
            nxt = earliest_free(spans, occupy, start, [(u.available_at, _FAR)], 0, latest)
            clash = _clash(spans, start, start + occupy)
            if u.available_at > start:
                word = _STATUS_WORDS.get(u.status, "not available")
                states.append(ResourceStatus(label, False, f"{word} until {fmt(u.available_at)}", nxt))
            elif clash:
                note = _clash_note(
                    clash, start, "in use by {case}, back from CSSD {end}", "needed by {case} at {start}"
                )
                states.append(ResourceStatus(label, False, note, nxt))
            else:
                states.append(ResourceStatus(label, True, "ready", start))
        items.extend(states)
        if sum(s.ok for s in states) >= count:
            readies.append(start)
        else:
            ok = False
            readies.append(_kth_ready(states, count))
    return AgentVerdict(EQUIPMENT, ok, items, _combine(readies))


def _facility(opt, case: SurgeryCase, start: int, busy: Busy) -> AgentVerdict:
    dur = case.duration_mins
    end = start + dur
    horizon = opt.horizon_mins
    latest = horizon - dur
    items: list[ResourceStatus] = []
    rooms = [r for r in opt.rooms if r.supports(case.required_capabilities)]
    if not rooms:
        need = ", ".join(sorted(case.required_capabilities))
        items.append(ResourceStatus("Rooms", False, f"no room with [{need}]"))
    room_states = []
    for r in rooms:
        turnover = r.turnover_for(case)
        if not r.is_usable:
            room_states.append(ResourceStatus(r.or_id, False, r.status.lower()))
            continue
        spans = busy.room.get(r.or_id, [])
        nxt = earliest_free(spans, dur + turnover, start, [(r.open_min, r.close(horizon))], dur, latest)
        clash = _clash(spans, start, end + turnover)
        if start < r.open_min or end > r.close(horizon):
            room_states.append(ResourceStatus(r.or_id, False, "closed at that time", nxt))
        elif clash:
            note = _clash_note(clash, start, "busy with {case}, clean at {end}", "booked for {case} at {start}")
            room_states.append(ResourceStatus(r.or_id, False, note, nxt))
        else:
            room_states.append(
                ResourceStatus(r.or_id, True, f"free {fmt(start)}-{fmt(end + turnover)} incl. {turnover} min clean", start)
            )
    items.extend(room_states)
    room_ok = any(s.ok for s in room_states)
    readies = [start if room_ok else _kth_ready(room_states, 1)]

    bed_ok = True
    if opt.beds:
        bed_type, rec = case.recovery_bed_type, case.recovery_duration
        pool = [b for b in opt.beds if b.bed_type == bed_type]
        free, nexts = [], []
        for b in pool:
            spans = busy.bed.get(b.bed_id, [])
            shifted = [(s - dur, e - dur, c) for s, e, c in spans]
            nxt = earliest_free(shifted, rec, start, [(b.available_from - dur, _FAR)], 0, latest)
            if nxt == start:
                free.append(b.bed_id)
            if nxt is not None:
                nexts.append(nxt)
        bed_ok = bool(free)
        if free:
            items.append(ResourceStatus(f"{bed_type} bed", True, f"{len(free)} free from {fmt(end)} ({', '.join(free)})", start))
            readies.append(start)
        else:
            ready = min(nexts, default=None)
            when = f"first frees for a {fmt(ready)} start" if ready is not None else "none today"
            items.append(ResourceStatus(f"{bed_type} bed", False, f"all busy at {fmt(end)}, {when}", ready))
            readies.append(ready)
    return AgentVerdict(FACILITY, room_ok and bed_ok, items, _combine(readies))
