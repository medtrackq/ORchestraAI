"""Constraint optimisation engine of the Master Orchestrator (OR-Tools CP-SAT).

Flow for every solve:

1. Resource agents (``_plan_case``) list what each case could use: rooms
   (OR Facility), team members and their shifts (Staffing), equipment units
   (Equipment & CSSD) and recovery beds. A case that can never be placed is
   reported with the reasons instead of reaching the solver.
2. ``_ModelBuilder`` turns the plans into one CP-SAT model:
   hard constraints = no overlap per room (incl. turnover), per person, per
   equipment unit (incl. sterilisation) and per recovery bed, shifts, room
   hours, capabilities, skills, release times, deadlines and the fatigue
   limits (weekly and daily hours, rest after a late-night case; see
   ``fatigue``), which EMERGENCY cases may break at a high cost;
   soft constraints = priority by urgency, surgeon's preferred start,
   grouping cases with similar equipment, team synergy, spreading work to
   the least tired staff and, when rescheduling, keeping changes to the
   previous schedule small.
3. The solution is decoded into ``Assignment`` rows with a short explanation
   of what each case waited for.

``reschedule`` keeps every case that has already started locked in place and
re-optimises the rest; ``insert_emergency`` and ``handle_overrun`` are
shortcuts for the two common triggers.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Iterable, Mapping

from ortools.sat.python import cp_model

from .agents import EQUIPMENT as EQUIPMENT_AGENT
from .agents import FACILITY, Busy, SlotCheck, scan_slot
from .agents import STAFFING as STAFFING_AGENT
from .fatigue import day_minutes, fatigue_issues, late_windows, load_percent
from .models import EquipmentUnit, OperatingRoom, RecoveryBed, StaffMember, SupplyItem, SurgeryCase, Urgency
from .results import Assignment, Notification, ScheduleChange, ScheduleResult
from .timeutil import MINUTES_PER_DAY, fmt

_URGENCY_RANK = {Urgency.EMERGENCY: 0, Urgency.URGENT: 1, Urgency.ELECTIVE: 2}


@dataclass(frozen=True)
class Weights:
    """Objective weights. One unit is roughly one minute of elective delay."""

    unscheduled_emergency: int = 1_000_000
    unscheduled_urgent: int = 100_000
    unscheduled_elective: int = 10_000
    emergency_wait_per_min: int = 200
    urgent_wait_per_min: int = 20
    preferred_start_dev_per_min: int = 2
    elective_start_per_min: int = 1  # keeps days compact when no preference
    setup_switch: int = 60  # next case in the room needs a different set-up
    reschedule_shift_per_min: int = 3
    room_change: int = 120
    staff_change: int = 40
    equipment_change: int = 5  # swapping identical units/beds still means a call
    bed_change: int = 5
    synergy_per_point: int = 10  # bonus per synergy_score point (0-5)
    fatigue_spread: int = 40  # cost of picking someone at 100% fatigue (0% = free)
    day_spread_after_mins: int = 240  # beyond this much OR time in a day ...
    day_spread_per_quarter: int = 2  # ... each extra 15 min on one person costs this (spreads the day)
    fatigue_override: int = 2000  # an EMERGENCY case breaks one person's fatigue limit
    fatigue_over_per_min: int = 10  # per minute over a weekly / daily hour limit
    other_room: int = 30  # a case placed outside its preferred rooms (its surgeon's own OR block)


@dataclass
class _Plan:
    """What the resource agents found possible for one case."""

    case: SurgeryCase
    earliest: int
    latest_start: int
    rooms: list[OperatingRoom]
    surgeon_id: str
    team: dict[str, tuple[int, list[StaffMember]]]  # role -> (count, candidates)
    windows: dict[str, list[tuple[int, int]]] | None  # None: no shift check (locked)
    units: dict[str, tuple[int, list[EquipmentUnit]]]  # type -> (count, candidates)
    beds: list[RecoveryBed]
    frozen: bool = False
    existing: bool = False  # already booked (being re-planned), not a new request

    @property
    def duration(self) -> int:
        return self.case.duration_mins


@dataclass
class _Vars:
    start: cp_model.IntVar
    scheduled: cp_model.IntVar
    rooms: dict[str, cp_model.IntVar]
    staff: dict[str, cp_model.IntVar]  # surgeon maps to `scheduled`
    units: dict[str, cp_model.IntVar]
    beds: dict[str, cp_model.IntVar]


def _subtract(windows: list[tuple[int, int]], holes) -> list[tuple[int, int]]:
    """``windows`` minus ``holes`` (``(start, end, ...)`` spans)."""
    for hole in holes:
        h0, h1 = hole[0], hole[1]
        out = []
        for a, b in windows:
            if h1 <= a or h0 >= b:
                out.append((a, b))
                continue
            if a < h0:
                out.append((a, h0))
            if h1 < b:
                out.append((h1, b))
        windows = out
    return windows


def _open_windows(room: OperatingRoom, horizon: int, case: SurgeryCase | None = None) -> list[tuple[int, int]]:
    """When the room can be used (by ``case``): opening / elective hours minus planned closures
    (and minus other owners' OR blocks the case may not use)."""
    hours = room.hours_for(case, horizon) if case is not None else (room.open_min, room.close(horizon))
    blocks = case.room_blocks.get(room.or_id, ()) if case is not None else ()
    return _subtract([hours], list(room.closures) + list(blocks))


def _index(items: list, attr: str) -> dict:
    out = {}
    for item in items:
        key = getattr(item, attr)
        if key in out:
            raise ValueError(f"duplicate {attr}: {key}")
        out[key] = item
    return out


class ORchestraOptimizer:
    """Builds and solves the OR schedule for a set of surgery cases."""

    def __init__(
        self,
        rooms: Iterable[OperatingRoom],
        equipments: Iterable[EquipmentUnit],
        staff: Iterable[StaffMember],
        beds: Iterable[RecoveryBed] = (),
        *,
        team_synergy: Mapping[tuple[str, str], float] | None = None,
        horizon_mins: int = MINUTES_PER_DAY,
        weights: Weights | None = None,
        time_limit_s: float = 10.0,
        num_workers: int = 8,
        enable_setup_grouping: bool = True,
        random_seed: int = 0,
        supplies: Iterable[SupplyItem] = (),
    ) -> None:
        if horizon_mins <= 0:
            raise ValueError("horizon_mins must be positive")
        self.rooms = list(rooms)
        self.equipments = list(equipments)
        self.staff = list(staff)
        self.beds = list(beds)
        self.team_synergy = dict(team_synergy or {})
        self.horizon_mins = horizon_mins
        self.weights = weights or Weights()
        self.time_limit_s = time_limit_s
        self.num_workers = num_workers
        self.enable_setup_grouping = enable_setup_grouping
        self.random_seed = random_seed
        self._room_by_id = _index(self.rooms, "or_id")
        self._unit_by_id = _index(self.equipments, "unit_id")
        self._staff_by_id = _index(self.staff, "staff_id")
        self._bed_by_id = _index(self.beds, "bed_id")
        self.supplies = list(supplies)
        self._supply_by_id = _index(self.supplies, "item_id")

    # ------------------------------------------------------------------ API

    def solve_schedule(
        self,
        cases: Iterable[SurgeryCase],
        scenario_name: str = "Schedule",
        *,
        now_min: int = 0,
        verbose: bool = True,
    ) -> ScheduleResult:
        """Schedule ``cases`` from scratch. Nothing may start before ``now_min``."""
        result = self._solve(list(cases), scenario_name, now_min=now_min, frozen={}, previous={})
        if verbose:
            print(result.report(), end="\n\n")
        return result

    def reschedule(
        self,
        current: ScheduleResult,
        cases: Iterable[SurgeryCase],
        *,
        now_min: int,
        new_cases: Iterable[SurgeryCase] = (),
        duration_updates: Mapping[str, int] | None = None,
        scenario_name: str = "Reschedule",
        verbose: bool = True,
    ) -> ScheduleResult:
        """Re-optimise ``current`` at time ``now_min`` (ripple effect).

        Cases that started before ``now_min`` stay locked to their room, team
        and equipment (only their duration may change). Everything else may
        move, at a cost for every minute, room or person changed, so the
        result disturbs the existing plan as little as possible.
        """
        updates = dict(duration_updates or {})
        by_id: dict[str, SurgeryCase] = {}
        for case in [*cases, *new_cases]:
            if case.case_id in by_id:
                raise ValueError(f"duplicate case_id: {case.case_id}")
            by_id[case.case_id] = case
        missing = set(current.assignments) - set(by_id)
        if missing:
            raise ValueError(f"cases missing for scheduled ids: {sorted(missing)}")
        for cid, mins in updates.items():
            if cid not in by_id:
                raise ValueError(f"unknown case in duration_updates: {cid}")
            by_id[cid] = replace(by_id[cid], duration_mins=mins)

        frozen = {cid: a for cid, a in current.assignments.items() if a.start_min < now_min}
        result = self._solve(
            list(by_id.values()), scenario_name, now_min=now_min, frozen=frozen,
            previous=current.assignments,
        )
        result.changes, result.notifications = self._diff(current, result, updates)
        if verbose:
            print(result.report(), end="\n\n")
        return result

    def new_fatigue(self, before: ScheduleResult, after: ScheduleResult) -> list:
        """Fatigue issues in ``after`` that ``before`` did not have."""
        known = {i.key for i in self.fatigue(before.assignments, before.cases)}
        return [i for i in self.fatigue(after.assignments, after.cases) if i.key not in known]

    def insert_emergency(
        self,
        current: ScheduleResult,
        cases: Iterable[SurgeryCase],
        emergency_cases: Iterable[SurgeryCase],
        *,
        now_min: int,
        scenario_name: str = "Emergency insert",
        verbose: bool = True,
    ) -> ScheduleResult:
        """Fit new EMERGENCY/URGENT cases in, pushing back less urgent ones."""
        return self.reschedule(
            current, cases, now_min=now_min, new_cases=emergency_cases,
            scenario_name=scenario_name, verbose=verbose,
        )

    def handle_overrun(
        self,
        current: ScheduleResult,
        cases: Iterable[SurgeryCase],
        case_id: str,
        *,
        new_duration_mins: int,
        now_min: int,
        scenario_name: str = "Overrun",
        verbose: bool = True,
    ) -> ScheduleResult:
        """A case will run longer (or shorter) than planned: ripple the rest."""
        if case_id not in current.assignments:
            raise ValueError(f"{case_id} is not in the current schedule")
        return self.reschedule(
            current, cases, now_min=now_min, duration_updates={case_id: new_duration_mins},
            scenario_name=scenario_name, verbose=verbose,
        )

    def check_slot(
        self,
        current: ScheduleResult,
        case: SurgeryCase,
        start_min: int,
        *,
        scenario_name: str | None = None,
        verbose: bool = True,
    ) -> SlotCheck:
        """Ask for ``case`` at ``start_min`` without moving anything already booked.

        1. Parallel scan: each resource agent checks the requested time.
        2. All agree: lock the case at that time.
        3. Otherwise (negotiation): CP-SAT finds the earliest slot at or
           after the requested time where every agent agrees.
        """
        if case.case_id in current.assignments:
            raise ValueError(f"{case.case_id} is already booked")
        dur, end = case.duration_mins, start_min + case.duration_mins
        verdicts = scan_slot(self, case, start_min, Busy.from_assignments(current.assignments, current.cases))
        ok = all(v.ok for v in verdicts)
        name = self._staff_by_id[case.surgeon_id].name if case.surgeon_id in self._staff_by_id else case.surgeon_id
        log = [
            f"[Intake -> Master] {case.case_id} {case.procedure} | {name} | {case.urgency.value} | "
            f"{dur} min | requested {fmt(start_min)}",
            f"[Master Orchestrator] parallel scan {fmt(start_min)}-{fmt(end)} -> Staffing, Equipment & CSSD, OR Facility",
        ]
        for v in verdicts:
            log.append(f"[{v.agent}] {'OK' if v.ok else 'CONFLICT'}: {v.summary()}")

        booked = [current.cases[cid] for cid in current.assignments]
        attempts = []
        if ok:
            attempts.append(replace(case, release_min=max(case.release_min, start_min),
                                    preferred_start=None, deadline_min=start_min))
        else:
            rejected = ", ".join(v.agent for v in verdicts if not v.ok)
            hints = ", ".join(
                f"{v.agent} from {fmt(v.ready_at)}" if v.ready_at is not None else f"{v.agent}: not today"
                for v in verdicts if not v.ok
            )
            log.append(f"[Master Orchestrator] {fmt(start_min)} rejected by {rejected}. Earliest ready: {hints}")
            log.append("[Master Orchestrator] running CP-SAT for the next slot where all agents agree")
        attempts.append(replace(case, release_min=max(case.release_min, start_min), preferred_start=None))

        probe_weights = replace(
            self.weights,
            elective_start_per_min=100, urgent_wait_per_min=100,
            emergency_wait_per_min=max(self.weights.emergency_wait_per_min, 100),
            unscheduled_elective=10**7, unscheduled_urgent=10**7, unscheduled_emergency=10**8,
        )
        title = scenario_name or f"Slot check {case.case_id} @ {fmt(start_min)}"
        for probe in attempts:
            result = self._solve(
                [*booked, probe], title, now_min=current.now_min, frozen=dict(current.assignments),
                previous=current.assignments, weights=probe_weights, lock_beds=True,
            )
            if case.case_id in result.assignments:
                break
        # Existing bookings are untouched: keep their original records.
        result.assignments = {
            cid: current.assignments.get(cid, a) for cid, a in result.assignments.items()
        }
        reasons = result.unscheduled.get(case.case_id)
        result.cases = {**current.cases, case.case_id: case}
        result.fatigue = self.fatigue(result.assignments, result.cases)
        result.unscheduled = dict(current.unscheduled)
        if reasons:
            result.unscheduled[case.case_id] = reasons
        result.changes, result.notifications = self._diff(current, result, {})

        placed = result.assignments.get(case.case_id)
        if placed is None:
            log.append("[Master Orchestrator] no conflict-free slot today: " + "; ".join(result.unscheduled[case.case_id]))
        else:
            where = f"{placed.or_id} {fmt(placed.start_min)}-{fmt(placed.end_min)}"
            log.append(f"[Master Orchestrator] {'locked' if placed.start_min == start_min else 'proposed and locked'} {where}"
                       + (f" ({'; '.join(placed.reasoning)})" if placed.reasoning else ""))
            if placed.equipment_unit_ids:
                ready = ", ".join(f"{u} back {fmt(t)}" for u, t in placed.equipment_ready_min)
                log.append(f"[{EQUIPMENT_AGENT}] reserved {', '.join(placed.equipment_unit_ids)}; CSSD queue: {ready}")
            if placed.bed_id:
                log.append(f"[{FACILITY}] reserved {placed.bed_id} from {fmt(placed.recovery_start_min)}")
            team = ", ".join(self._staff_by_id[s].name for s in placed.staff_ids)
            log.append(f"[{STAFFING_AGENT}] notified team: {team}")
            for issue in result.fatigue:
                if issue.case_id == case.case_id and issue.override:
                    log.append(f"[{STAFFING_AGENT}] FATIGUE OVERRIDE: {issue.detail()}")
        check = SlotCheck(case, start_min, verdicts, result, log)
        if verbose:
            print(f"=== {title} ===\n{check.report()}", end="\n\n")
        return check

    # --------------------------------------------------------------- agents

    def _plan_case(
        self, case: SurgeryCase, now_min: int, *, existing: bool = False
    ) -> tuple[_Plan | None, list[str]]:
        """Resource agents' scan: candidates per resource, or why there are none."""
        horizon = self.horizon_mins
        dur = case.duration_mins
        earliest = max(case.release_min, now_min, 0)
        latest_start = horizon - dur
        if case.deadline_min is not None:
            latest_start = min(latest_start, case.deadline_min)
        if earliest > latest_start:
            return None, [
                f"Time: earliest start {fmt(earliest)} is after the latest allowed start "
                f"{fmt(latest_start)} (deadline or end of horizon)"
            ]
        latest_end = latest_start + dur

        def fits(window_start: int, window_end: int) -> bool:
            return max(window_start, earliest) + dur <= min(window_end, latest_end)

        reasons: list[str] = []

        # OR Facility agent: room capabilities, status, opening hours and planned closures.
        capable = [r for r in self.rooms if r.supports(case.required_capabilities)]
        rooms = [r for r in capable if r.is_usable and any(fits(a, b) for a, b in _open_windows(r, horizon, case))]
        elective_only = [r for r in capable if r.is_usable and r not in rooms
                         and any(fits(a, b) for a, b in _open_windows(r, horizon))]
        blocked = [r for r in capable if r.is_usable and r not in rooms and case.room_blocks.get(r.or_id)
                   and any(fits(a, b) for a, b in _subtract([r.hours_for(case, horizon)], r.closures))]
        if not rooms and blocked:
            spans = "; ".join(f"{r.or_id} " + ", ".join(f"{fmt(a)}-{fmt(b)} {why}" for a, b, why in case.room_blocks[r.or_id])
                              for r in blocked)
            reasons.append(f"Facility: the free time is another owner's OR block ({spans}); "
                           "book another day, the owner's release, or mark it URGENT if it cannot wait")
        elif not rooms and elective_only:
            hours = "; ".join(f"{r.or_id} {fmt(r.hours_for(case, horizon)[0])}-{fmt(r.hours_for(case, horizon)[1])}"
                              for r in elective_only)
            reasons.append(f"Facility: an elective case of {dur} min does not fit the elective hours ({hours}); "
                           "book another day or mark it URGENT if it cannot wait")
        elif not rooms:
            need = ", ".join(sorted(case.required_capabilities)) or "any"
            shut = [
                f"{r.or_id} {r.status}" if not r.is_usable else
                f"{r.or_id} closed " + ", ".join(f"{fmt(a)}-{fmt(b)}" + (f" ({why})" if why else "")
                                                 for a, b, why in r.closures)
                for r in capable if not r.is_usable or r.closures
            ]
            if not capable:
                reasons.append(f"Facility: no room with [{need}]")
            elif shut:
                reasons.append(f"Facility: rooms with [{need}] are closed or under maintenance: {'; '.join(shut)}")
            else:
                reasons.append(f"Facility: no usable room with [{need}] open long enough for {dur} min")

        # Staffing agent: shifts, skills and fatigue limits. EMERGENCY cases
        # may break fatigue limits (at a cost in the solver), normal cases not.
        emergency = case.urgency is Urgency.EMERGENCY

        def shift_fit(person: StaffMember) -> list[tuple[int, int]]:
            spans = person.shifts
            if not emergency and person.rest_until > 0:  # resting after another day's late case
                spans = tuple((max(a, person.rest_until), b) for a, b in spans if b > person.rest_until)
            return [w for w in spans if fits(*w)]

        def over_limit(person: StaffMember) -> str | None:
            if emergency:
                return None
            if person.remaining_minutes < dur:
                return f"would exceed {person.max_weekly_hours:g} h in 7 days ({person.remaining_minutes} min left)"
            if person.daily_cap_minutes < dur:
                return f"case is longer than the {person.max_daily_hours:g} h daily limit"
            return None

        windows: dict[str, list[tuple[int, int]]] = {}
        surgeon = self._staff_by_id.get(case.surgeon_id)
        if surgeon is None or not surgeon.is_active:
            reasons.append(f"Staffing: surgeon {case.surgeon_id} not found or inactive")
        else:
            fit, limit = shift_fit(surgeon), over_limit(surgeon)
            if not fit:
                resting = not emergency and surgeon.rest_until > earliest and [w for w in surgeon.shifts if fits(*w)]
                reasons.append(
                    f"Staffing: {surgeon.name} rests until {fmt(surgeon.rest_until)} after a late case"
                    if resting else
                    f"Staffing: {surgeon.name} has no shift that fits {dur} min "
                    f"between {fmt(earliest)} and {fmt(latest_end)}"
                )
            elif limit:
                reasons.append(f"Staffing: {surgeon.name} {limit}")
            else:
                windows[surgeon.staff_id] = fit
        team: dict[str, tuple[int, list[StaffMember]]] = {}
        for role, count in case.required_staff_roles.items():
            if count == 0:
                continue
            skills = case.required_staff_skills.get(role, frozenset())
            skilled = [
                p for p in self.staff
                if p.staff_id != case.surgeon_id and p.is_active and p.role == role and skills <= p.skills
            ]
            candidates = []
            for person in skilled:
                fit = shift_fit(person)
                if fit and not over_limit(person):
                    candidates.append(person)
                    windows[person.staff_id] = fit
            if len(candidates) < count:
                label = role + (f" with {', '.join(sorted(skills))}" if skills else "")
                if len(skilled) < count:
                    reasons.append(
                        f"Staffing: need {count} {label}, only {len(skilled)} active staff "
                        + ("have the skill" if skills else "in this role")
                    )
                else:
                    reasons.append(
                        f"Staffing: need {count} {label} on shift, rested and within the daily/7-day hour "
                        f"limits, found {len(candidates)} of {len(skilled)}"
                    )
            team[role] = (count, candidates)

        # Equipment & CSSD agent: unit status and when it is back from CSSD.
        units: dict[str, tuple[int, list[EquipmentUnit]]] = {}
        for eq_type, count in sorted(case.equipment_counts.items()):
            candidates = [
                u for u in self.equipments
                if u.eq_type == eq_type and u.is_usable and u.available_at <= latest_start
                and any(fits(a, b) for a, b in _subtract([(u.available_at, horizon)], u.closures))
            ]
            if len(candidates) < count:
                serviced = [
                    f"{u.unit_id} " + ", ".join(f"{fmt(a)}-{fmt(b)}" + (f" ({why})" if why else "") for a, b, why in u.closures)
                    for u in self.equipments if u.eq_type == eq_type and u.closures
                ]
                reasons.append(
                    f"Equipment: need {count} x {eq_type} ready by {fmt(latest_start)}, found {len(candidates)}"
                    + (f" (planned maintenance: {'; '.join(serviced)})" if serviced else "")
                )
            units[eq_type] = (count, candidates)

        # Equipment & Inventory agent: single-use supplies and implants in stock.
        emergency_stock = case.urgency is Urgency.EMERGENCY  # may take what later bookings hold
        for code, qty in sorted(case.required_supplies.items()):
            item = self._supply_by_id.get(code)
            if item is None:
                reasons.append(f"Inventory: {code} is not a stocked item")
            elif (item.for_booked if existing or emergency_stock else item.available) < qty:
                have = item.for_booked if existing or emergency_stock else item.available
                reasons.append(
                    f"Inventory: need {qty} x {item.name} ({code}), only {have} available"
                    + (f"; {item.note}" if item.note else "")
                )

        # OR Facility agent: recovery bed (PACU / ICU) right after surgery.
        beds: list[RecoveryBed] = []
        if self.beds:
            # The case cannot end after the surgeon's last shift that fits it.
            surgeon_end = max((b for _, b in windows.get(case.surgeon_id, [])), default=latest_end)
            bed_by = min(latest_end, surgeon_end)
            beds = [
                b for b in self.beds
                if b.bed_type == case.recovery_bed_type and b.is_usable and b.available_from <= bed_by
            ]
            if not beds:
                closed = [b.bed_id for b in self.beds if b.bed_type == case.recovery_bed_type and not b.is_usable]
                taken = [f"{b.bed_id} until {fmt(b.available_from)}" for b in self.beds
                         if b.bed_type == case.recovery_bed_type and b.is_usable and b.available_from > bed_by]
                reasons.append(f"Facility: no {case.recovery_bed_type} bed free by {fmt(bed_by)}"
                               + (f" (occupied: {', '.join(taken)})" if taken else "")
                               + (f" (closed: {', '.join(closed)})" if closed else ""))

        if reasons:
            return None, reasons
        return _Plan(case, earliest, latest_start, rooms, case.surgeon_id, team, windows, units, beds,
                     existing=existing), []

    def _frozen_plan(self, case: SurgeryCase, a: Assignment, now_min: int, lock_bed: bool = False) -> _Plan:
        """A case already in progress: same room, start, team and equipment."""
        try:
            room = self._room_by_id[a.or_id]
            members = [self._staff_by_id[sid] for sid in a.staff_ids[1:]]
            used_units = [self._unit_by_id[uid] for uid in a.equipment_unit_ids]
        except KeyError as exc:
            raise ValueError(f"{case.case_id}: locked assignment refers to unknown resource {exc}") from exc
        team: dict[str, list[StaffMember]] = defaultdict(list)
        for person in members:
            team[person.role].append(person)
        units: dict[str, list[EquipmentUnit]] = defaultdict(list)
        for unit in used_units:
            units[unit.eq_type].append(unit)
        beds: list[RecoveryBed] = []
        if a.bed_id is not None and self.beds:
            ended_as_planned = a.end_min == a.start_min + case.duration_mins
            in_bed = a.recovery_start_min is not None and a.recovery_start_min <= now_min
            if lock_bed or (ended_as_planned and in_bed):
                beds = [self._bed_by_id[a.bed_id]]  # reserved / patient already in the bed
            else:
                beds = [b for b in self.beds if b.bed_type == case.recovery_bed_type]
        return _Plan(
            case=case,
            earliest=a.start_min,
            latest_start=a.start_min,
            rooms=[room],
            surgeon_id=a.staff_ids[0],
            team={role: (len(p), p) for role, p in team.items()},
            windows=None,
            units={t: (len(u), u) for t, u in units.items()},
            beds=beds,
            frozen=True,
            existing=True,
        )

    # ---------------------------------------------------------------- solve

    def _solve(
        self,
        cases: list[SurgeryCase],
        scenario: str,
        *,
        now_min: int,
        frozen: Mapping[str, Assignment],
        previous: Mapping[str, Assignment],
        weights: Weights | None = None,
        lock_beds: bool = False,
    ) -> ScheduleResult:
        t0 = time.perf_counter()
        case_by_id: dict[str, SurgeryCase] = {}
        for case in cases:
            if case.case_id in case_by_id:
                raise ValueError(f"duplicate case_id: {case.case_id}")
            case_by_id[case.case_id] = case

        unscheduled: dict[str, list[str]] = {}
        plans: dict[str, _Plan] = {}
        for cid, case in case_by_id.items():
            if cid in frozen:
                plans[cid] = self._frozen_plan(case, frozen[cid], now_min, lock_bed=lock_beds)
                continue
            plan, reasons = self._plan_case(case, now_min, existing=cid in previous)
            if plan is None:
                unscheduled[cid] = reasons
            else:
                plans[cid] = plan

        builder = _ModelBuilder(self, previous, weights or self.weights, frozen)
        for cid, plan in plans.items():
            builder.add_case(plan)
        builder.add_resource_constraints()
        if self.enable_setup_grouping:
            builder.add_setup_grouping()
        if builder.objective:
            builder.model.minimize(sum(builder.objective))

        assignments: dict[str, Assignment] = {}
        objective: float | None = None
        status_name = "OPTIMAL"
        if plans:
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = self.time_limit_s
            solver.parameters.num_workers = self.num_workers
            solver.parameters.random_seed = self.random_seed
            status = solver.solve(builder.model)
            status_name = solver.status_name(status)
            if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                objective = solver.objective_value
                for cid, plan in plans.items():
                    v = builder.vars[cid]
                    if solver.boolean_value(v.scheduled):
                        assignments[cid] = self._extract(solver, plan, v)
                for cid, plan in plans.items():
                    if cid not in assignments:
                        unscheduled[cid] = self._supply_shortage(plan, assignments, case_by_id) or [
                            self._capacity_reason(plan)
                        ]
                assignments = self._explain(assignments, plans)
            else:
                why = " (locked in-progress cases cannot all fit)" if frozen else ""
                for cid in plans:
                    unscheduled[cid] = [f"Solver returned {status_name}{why}"]

        ordered = sorted(unscheduled, key=lambda c: (_URGENCY_RANK[case_by_id[c].urgency], c))
        return ScheduleResult(
            scenario=scenario,
            status=status_name,
            objective=objective,
            assignments=dict(sorted(assignments.items(), key=lambda kv: (kv[1].start_min, kv[1].or_id))),
            unscheduled={cid: unscheduled[cid] for cid in ordered},
            cases=case_by_id,
            staff_names={s.staff_id: s.name for s in self.staff},
            wall_time_s=time.perf_counter() - t0,
            now_min=now_min,
            fatigue=self.fatigue(assignments, case_by_id),
        )

    def fatigue(self, assignments: Mapping[str, Assignment], cases: Mapping[str, SurgeryCase]) -> list:
        """Fatigue-limit breaks in ``assignments`` (EMERGENCY overrides and history)."""
        return fatigue_issues(assignments, cases, self.staff, horizon_mins=self.horizon_mins)

    def _extract(self, solver: cp_model.CpSolver, plan: _Plan, v: _Vars) -> Assignment:
        case = plan.case
        start = solver.value(v.start)
        end = start + plan.duration
        room = next(r for r in plan.rooms if solver.boolean_value(v.rooms[r.or_id]))
        staff_ids = [plan.surgeon_id] + [
            sid for sid, lit in v.staff.items() if sid != plan.surgeon_id and solver.boolean_value(lit)
        ]
        units = [uid for uid, lit in v.units.items() if solver.boolean_value(lit)]
        bed = next((bid for bid, lit in v.beds.items() if solver.boolean_value(lit)), None)
        return Assignment(
            case_id=case.case_id,
            or_id=room.or_id,
            start_min=start,
            end_min=end,
            cleanup_end_min=end + room.turnover_for(case),
            staff_ids=tuple(staff_ids),
            equipment_unit_ids=tuple(units),
            equipment_ready_min=tuple((u, end + self._unit_by_id[u].reprocess_mins) for u in units),
            bed_id=bed,
            recovery_start_min=end if bed else None,
            recovery_end_min=end + case.recovery_duration if bed else None,
            frozen=plan.frozen,
        )

    def _supply_shortage(self, plan: _Plan, assignments: Mapping[str, Assignment],
                         cases: Mapping[str, SurgeryCase]) -> list[str]:
        """Stock the scheduled cases used up, leaving too little for this one."""
        out = []
        for code, qty in sorted(plan.case.required_supplies.items()):
            item = self._supply_by_id[code]
            have = item.for_booked if plan.existing or plan.case.urgency is Urgency.EMERGENCY else item.available
            used = sum(cases[cid].required_supplies.get(code, 0) for cid in assignments)
            if used + qty > have:
                out.append(
                    f"Inventory: {item.name} ({code}) runs out: {used} of {have} go to other cases "
                    f"today, need {qty}" + (f"; {item.note}" if item.note else "")
                )
        return out

    def _capacity_reason(self, plan: _Plan) -> str:
        window = f"between {fmt(plan.earliest)} and {fmt(plan.latest_start)}"
        if plan.case.urgency is Urgency.EMERGENCY:
            return (
                f"No conflict-free slot {window}: every eligible room/team/equipment/bed is held by "
                "locked or higher-priority cases. Escalate: open another room or call in a team"
            )
        return (
            f"No conflict-free slot left {window} with the current room, staff, equipment "
            "and bed load. Postpone to another day"
        )

    @staticmethod
    def _setup_cost(a: SurgeryCase, b: SurgeryCase, weights: Weights) -> int:
        pa, pb = a.equipment_profile, b.equipment_profile
        if pa == pb:
            return 0
        overlap = len(pa & pb) / len(pa | pb)
        return round(weights.setup_switch * (1 - overlap))

    # ------------------------------------------------------------- explain

    def _explain(self, assignments: dict[str, Assignment], plans: dict[str, _Plan]) -> dict[str, Assignment]:
        """Attach a short "why this slot" log to every new or moved case."""
        out = {}
        for cid, a in assignments.items():
            plan = plans[cid]
            if a.frozen:
                out[cid] = a
                continue
            case = plan.case
            notes = []
            if case.urgency is not Urgency.ELECTIVE:
                notes.append(
                    f"{case.urgency.value}: starts {a.start_min - plan.earliest} min after "
                    f"it could first start ({fmt(plan.earliest)})"
                )
            on_preferred = case.preferred_start == a.start_min
            if on_preferred:
                notes.append("on the surgeon's preferred start time")
            elif case.preferred_start is not None:
                delta = a.start_min - case.preferred_start
                side = "after" if delta > 0 else "before"
                notes.append(f"{abs(delta)} min {side} preferred start {fmt(case.preferred_start)}")
            if a.start_min > plan.earliest and not on_preferred:
                blockers = self._blockers(a, assignments, plan)
                if blockers:
                    notes.append("waits until " + "; ".join(blockers))
            if case.deadline_min is not None:
                notes.append(f"meets deadline {fmt(case.deadline_min)}")
            out[cid] = replace(a, reasoning=tuple(notes))
        return out

    def _blockers(self, a: Assignment, assignments: dict[str, Assignment], plan: _Plan) -> list[str]:
        """Resources that became free exactly when ``a`` starts."""
        found = []
        others = [b for b in assignments.values() if b.case_id != a.case_id]
        room = self._room_by_id[a.or_id]
        for b in others:
            if b.or_id == a.or_id and b.cleanup_end_min == a.start_min:
                found.append(f"{a.or_id} is cleaned after {b.case_id}")
        opens = room.hours_for(plan.case, self.horizon_mins)[0]
        if opens == a.start_min and opens > 0:
            found.append(f"{a.or_id} opens" + (" for elective cases" if opens != room.open_min else ""))
        for _, end, why in room.closures:
            if end == a.start_min:
                found.append(f"{a.or_id} reopens after closure" + (f" ({why})" if why else ""))
        shift_starts = []
        for sid in a.staff_ids:
            name = self._staff_by_id[sid].name
            for b in others:
                if sid in b.staff_ids and b.end_min == a.start_min:
                    found.append(f"{name} finishes {b.case_id}")
            if any(start == a.start_min for start, _ in self._staff_by_id[sid].shifts):
                shift_starts.append(name)
        if shift_starts:
            found.append(f"shift starts at {fmt(a.start_min)} for {', '.join(shift_starts)}")
        for uid in a.equipment_unit_ids:
            unit = self._unit_by_id[uid]
            for b in others:
                if any(u == uid and ready == a.start_min for u, ready in b.equipment_ready_min):
                    found.append(f"{uid} is back from CSSD after {b.case_id}")
            if unit.available_at == a.start_min and unit.available_at > 0:
                found.append(f"{uid} is released by CSSD")
        if a.bed_id is not None:
            bed = self._bed_by_id[a.bed_id]
            for b in others:
                if b.bed_id == a.bed_id and b.recovery_end_min == a.end_min:
                    found.append(f"{a.bed_id} frees up after {b.case_id}")
            if bed.available_from == a.end_min and bed.available_from > 0:
                found.append(f"{a.bed_id} becomes free")
        return found

    # ---------------------------------------------------------------- diff

    def _diff(
        self, old: ScheduleResult, new: ScheduleResult, updates: Mapping[str, int]
    ) -> tuple[list[ScheduleChange], list[Notification]]:
        names = new.staff_names
        changes: list[ScheduleChange] = []
        notes: list[Notification] = []
        for cid in sorted(set(old.assignments) | set(new.assignments)):
            a, b = old.assignments.get(cid), new.assignments.get(cid)
            details: list[str] = []
            if a is not None and b is None:
                why = "; ".join(new.unscheduled.get(cid, []))
                details.append(f"REMOVED (was {a.or_id} {fmt(a.start_min)}). {why}")
            elif b is not None and a is None:
                details.append(f"NEW booking {b.or_id} {fmt(b.start_min)}-{fmt(b.end_min)}")
            elif a is not None and b is not None:
                if cid in updates:
                    details.append(
                        f"duration {a.end_min - a.start_min}->{b.end_min - b.start_min} min, "
                        f"now ends {fmt(b.end_min)}"
                    )
                if a.start_min != b.start_min:
                    details.append(f"moved {fmt(a.start_min)}->{fmt(b.start_min)}")
                if a.or_id != b.or_id:
                    details.append(f"room {a.or_id}->{b.or_id}")
                gone = [names.get(s, s) for s in a.staff_ids if s not in b.staff_ids]
                came = [names.get(s, s) for s in b.staff_ids if s not in a.staff_ids]
                if gone or came:
                    details.append(f"team -[{', '.join(gone)}] +[{', '.join(came)}]")
                if set(a.equipment_unit_ids) != set(b.equipment_unit_ids):
                    details.append(f"equipment {list(a.equipment_unit_ids)}->{list(b.equipment_unit_ids)}")
                if a.bed_id != b.bed_id:
                    details.append(f"bed {a.bed_id}->{b.bed_id}")
            if not details:
                continue
            changes.append(ScheduleChange(cid, details, a, b))
            case = new.cases[cid]
            message = f"[ORchestraAI] {cid} {case.procedure} (HN {case.patient_hn}): " + "; ".join(details)
            people = dict.fromkeys((a.staff_ids if a else ()) + (b.staff_ids if b else ()))
            notes.extend(Notification(sid, cid, message) for sid in people)
        return changes, notes


class _ModelBuilder:
    """Accumulates CP-SAT variables, intervals and objective terms."""

    def __init__(
        self,
        opt: ORchestraOptimizer,
        previous: Mapping[str, Assignment],
        weights: Weights,
        frozen: Mapping[str, Assignment] | None = None,
    ) -> None:
        self.opt = opt
        self.previous = previous  # schedule being revised (empty on a fresh solve)
        self.weights = weights
        self.model = cp_model.CpModel()
        self.objective: list = []
        self.vars: dict[str, _Vars] = {}
        self.room_ivs: dict[str, list] = defaultdict(list)
        self.staff_ivs: dict[str, list] = defaultdict(list)
        self.unit_ivs: dict[str, list] = defaultdict(list)
        self.bed_ivs: dict[str, list] = defaultdict(list)
        self.room_members: dict[str, list] = defaultdict(list)
        self.frozen_rooms: dict[str, list[tuple[int, int]]] = defaultdict(list)  # cases already running
        self.frozen_units: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self.supply_load: dict[str, list[tuple[int, cp_model.IntVar, bool]]] = defaultdict(list)  # (qty, lit, new)
        self.frozen_supply: dict[str, int] = defaultdict(int)
        # Fatigue: (minutes, assigned literal, emergency override literal or None) per person.
        self.week_load: dict[str, list[tuple[int, cp_model.IntVar, cp_model.IntVar | None]]] = defaultdict(list)
        self.day_load: dict[str, list[tuple[int, cp_model.IntVar, cp_model.IntVar | None]]] = defaultdict(list)
        self.frozen_load: dict[str, int] = defaultdict(int)
        self.frozen_day_load: dict[str, int] = defaultdict(int)
        self.rest_work: dict[str, list] = defaultdict(list)  # case time that must not fall in a rest period
        self.rest_items: dict[str, list] = defaultdict(list)  # rest periods after late-night cases
        self._late_cache: dict[str, cp_model.IntVar] = {}  # case ends at night
        # Load already fixed before this solve, to prefer the least tired person.
        base_week: dict[str, int] = defaultdict(int)
        base_day: dict[str, int] = defaultdict(int)
        for a in (frozen or {}).values():
            for sid in a.staff_ids:
                base_week[sid] += a.end_min - a.start_min
                base_day[sid] += day_minutes(a.start_min, a.end_min, opt.horizon_mins)
        self.load_pct = {
            s.staff_id: load_percent(s, base_week[s.staff_id], base_day[s.staff_id]) for s in opt.staff
        }

    def add_case(self, p: _Plan) -> None:
        m = self.model
        case, cid, dur = p.case, p.case.case_id, p.duration
        horizon = self.opt.horizon_mins
        start = m.new_int_var(p.earliest, p.latest_start, f"start[{cid}]")
        scheduled = m.new_bool_var(f"scheduled[{cid}]")
        if p.frozen:
            m.add(scheduled == 1)

        # Rooms: occupancy includes turnover (cleaning) after the case.
        rooms = {}
        for room in p.rooms:
            x = m.new_bool_var(f"room[{cid},{room.or_id}]")
            rooms[room.or_id] = x
            occupied = dur + room.turnover_for(case)
            self.room_ivs[room.or_id].append(
                m.new_optional_fixed_size_interval_var(start, occupied, x, f"room_iv[{cid},{room.or_id}]")
            )
            self.room_members[room.or_id].append((p, x, start, occupied))
            if p.frozen:
                self.frozen_rooms[room.or_id].append((p.earliest, p.earliest + occupied))
            if not p.frozen:
                lo, hi = room.hours_for(case, horizon)  # elective cases: elective hours only
                if lo > p.earliest:
                    m.add(start >= lo).only_enforce_if(x)
                if hi < p.latest_start + dur:
                    m.add(start + dur <= hi).only_enforce_if(x)
                for i, (a, b, _) in enumerate(case.room_blocks.get(room.or_id, ())):  # another owner's OR block
                    before = m.new_bool_var(f"block_before[{cid},{room.or_id},{i}]")
                    m.add(start + occupied <= a).only_enforce_if([x, before])
                    m.add(start >= b).only_enforce_if([x, before.Not()])
                if case.preferred_rooms and room.or_id not in case.preferred_rooms:
                    self.objective.append(self.weights.other_room * x)
        m.add(sum(rooms.values()) == scheduled)

        # People: surgeon is fixed; other roles are chosen from candidates.
        staff = {p.surgeon_id: scheduled}
        for role, (count, candidates) in p.team.items():
            lits = []
            for person in candidates:
                y = m.new_bool_var(f"staff[{cid},{person.staff_id}]")
                staff[person.staff_id] = y
                lits.append(y)
            m.add(sum(lits) == count * scheduled)
        emergency = case.urgency is Urgency.EMERGENCY
        for sid, lit in staff.items():
            iv = m.new_optional_fixed_size_interval_var(start, dur, lit, f"staff_iv[{cid},{sid}]")
            self.staff_ivs[sid].append(iv)
            if p.windows is not None:
                self._add_shift(p, start, lit, sid, p.windows[sid])
            over = None
            if p.frozen:
                self.frozen_load[sid] += dur
                self.frozen_day_load[sid] += day_minutes(p.earliest, p.earliest + dur, horizon)
            else:
                if emergency:
                    # The emergency may break this person's fatigue limits, at a cost.
                    over = m.new_bool_var(f"fatigue_over[{cid},{sid}]")
                    strict = m.new_bool_var(f"rested[{cid},{sid}]")  # lit and not over
                    m.add_implication(over, lit)
                    m.add_implication(strict, lit)
                    m.add_implication(strict, ~over)
                    m.add_bool_or([~lit, over, strict])
                    self.objective.append(self.weights.fatigue_override * over)
                    iv = m.new_optional_fixed_size_interval_var(start, dur, strict, f"rested_iv[{cid},{sid}]")
                self.rest_work[sid].append(iv)
                self.week_load[sid].append((dur, lit, over))
                self.day_load[sid].append((dur, lit, over))
            self._add_rest(p, start, sid, lit, over)

        # Equipment: unit is busy for the case plus its CSSD reprocessing.
        units = {}
        for eq_type, (count, candidates) in p.units.items():
            lits = []
            for unit in candidates:
                z = m.new_bool_var(f"unit[{cid},{unit.unit_id}]")
                units[unit.unit_id] = z
                lits.append(z)
                self.unit_ivs[unit.unit_id].append(
                    m.new_optional_fixed_size_interval_var(
                        start, dur + unit.reprocess_mins, z, f"unit_iv[{cid},{unit.unit_id}]"
                    )
                )
                if p.frozen:
                    self.frozen_units[unit.unit_id].append((p.earliest, p.earliest + dur + unit.reprocess_mins))
                if not p.frozen and unit.available_at > p.earliest:
                    m.add(start >= unit.available_at).only_enforce_if(z)
            m.add(sum(lits) == count * scheduled)

        # Supplies and implants: shared stock for the whole horizon.
        for code, qty in case.required_supplies.items():
            if p.frozen:
                self.frozen_supply[code] += qty
            else:  # "new" = a new normal request; emergencies may take stock later bookings hold
                self.supply_load[code].append((qty, scheduled, not p.existing and case.urgency is not Urgency.EMERGENCY))

        # Recovery bed from the end of surgery.
        beds = {}
        if p.beds:
            for bed in p.beds:
                lit = m.new_bool_var(f"bed[{cid},{bed.bed_id}]")
                beds[bed.bed_id] = lit
                self.bed_ivs[bed.bed_id].append(
                    m.new_optional_fixed_size_interval_var(
                        start + dur, case.recovery_duration, lit, f"bed_iv[{cid},{bed.bed_id}]"
                    )
                )
                if not p.frozen and bed.available_from > p.earliest + dur:
                    m.add(start + dur >= bed.available_from).only_enforce_if(lit)
            m.add(sum(beds.values()) == scheduled)

        self.vars[cid] = _Vars(start, scheduled, rooms, staff, units, beds)
        self._add_objective(p, self.vars[cid])

    def _add_rest(self, p: _Plan, start, sid: str, lit, over) -> None:
        """Rest period after the case if it ends late at night (see ``fatigue``)."""
        person = self.opt._staff_by_id.get(sid)
        if person is None or person.rest_minutes <= 0:
            return
        m, cid, dur, rest = self.model, p.case.case_id, p.duration, person.rest_minutes
        lo, hi = p.earliest + dur, p.latest_start + dur
        night = late_windows(lo, hi)
        if not night:
            return
        end = start + dur
        if night == [(lo, hi)]:
            present = lit  # always ends at night
        else:
            late = self._late_var(p, start, night)
            present = m.new_bool_var(f"rest[{cid},{sid}]")
            m.add_implication(present, lit)
            m.add_implication(present, late)
            m.add_bool_or([~lit, ~late, present])
        self.rest_items[sid].append(
            m.new_optional_fixed_size_interval_var(end, rest, present, f"rest_iv[{cid},{sid}]")
        )
        nxt = person.next_booking_min
        if nxt is not None and not p.frozen and hi + rest > nxt:
            m.add(end + rest <= nxt).only_enforce_if([present] + ([~over] if over is not None else []))

    def _late_var(self, p: _Plan, start, night):
        cid = p.case.case_id
        if cid not in self._late_cache:
            late = self.model.new_bool_var(f"late[{cid}]")
            self._constrain_late(p, start + p.duration, late, night)
            self._late_cache[cid] = late
        return self._late_cache[cid]

    def _constrain_late(self, p: _Plan, end, late, night) -> None:
        lo, hi = p.earliest + p.duration, p.latest_start + p.duration
        day, cur = [], lo
        for a, b in night:
            if a > cur:
                day.append([cur, a - 1])
            cur = b + 1
        if cur <= hi:
            day.append([cur, hi])
        m = self.model
        at_night = cp_model.Domain.from_intervals([[a, b] for a, b in night])
        m.add_linear_expression_in_domain(end, at_night).only_enforce_if(late)
        m.add_linear_expression_in_domain(end, cp_model.Domain.from_intervals(day)).only_enforce_if(~late)

    def _add_shift(self, p: _Plan, start, lit, sid: str, windows: list[tuple[int, int]]) -> None:
        dur = p.duration
        if any(a <= p.earliest and b >= p.latest_start + dur for a, b in windows):
            return  # every possible start is inside this shift
        m = self.model
        if len(windows) == 1:
            (a, b), = windows
            m.add(start >= a).only_enforce_if(lit)
            m.add(start + dur <= b).only_enforce_if(lit)
            return
        picks = []
        for k, (a, b) in enumerate(windows):
            w = m.new_bool_var(f"shift[{p.case.case_id},{sid},{k}]")
            m.add(start >= a).only_enforce_if(w)
            m.add(start + dur <= b).only_enforce_if(w)
            picks.append(w)
        m.add(sum(picks) == lit)

    def _add_objective(self, p: _Plan, v: _Vars) -> None:
        m, w, case = self.model, self.weights, p.case
        base = self.previous.get(case.case_id)
        if base is not None and base.bed_id in v.beds:
            self.objective.append(w.bed_change * (1 - v.beds[base.bed_id]))
        if p.frozen:
            return
        bound = 2 * self.opt.horizon_mins
        unscheduled_cost = {
            Urgency.EMERGENCY: w.unscheduled_emergency,
            Urgency.URGENT: w.unscheduled_urgent,
            Urgency.ELECTIVE: w.unscheduled_elective,
        }[case.urgency]
        self.objective.append(unscheduled_cost * (1 - v.scheduled))

        wait = v.start - p.earliest
        if case.urgency is Urgency.EMERGENCY:
            self.objective.append(w.emergency_wait_per_min * wait)
        elif case.urgency is Urgency.URGENT:
            self.objective.append(w.urgent_wait_per_min * wait)
        elif case.preferred_start is None:
            self.objective.append(w.elective_start_per_min * wait)

        if case.preferred_start is not None and case.urgency is not Urgency.EMERGENCY:
            dev = m.new_int_var(0, bound + abs(case.preferred_start), f"pref_dev[{case.case_id}]")
            m.add_abs_equality(dev, v.start - case.preferred_start)
            self.objective.append(w.preferred_start_dev_per_min * dev)

        for sid, lit in v.staff.items():
            if sid == p.surgeon_id:
                continue
            score = self.opt.team_synergy.get((p.surgeon_id, sid))
            if score:
                self.objective.append(-round(w.synergy_per_point * score) * lit)
            tired = round(w.fatigue_spread * self.load_pct.get(sid, 0) / 100)
            if tired:
                self.objective.append(tired * lit)  # spread work to the least tired

        if base is not None:
            shift = m.new_int_var(0, bound + abs(base.start_min), f"moved[{case.case_id}]")
            m.add_abs_equality(shift, v.start - base.start_min)
            self.objective.append(w.reschedule_shift_per_min * shift)
            if base.or_id in v.rooms:
                self.objective.append(w.room_change * (1 - v.rooms[base.or_id]))
            for sid in base.staff_ids[1:]:
                if sid in v.staff:
                    self.objective.append(w.staff_change * (1 - v.staff[sid]))
            for uid in base.equipment_unit_ids:
                if uid in v.units:
                    self.objective.append(w.equipment_change * (1 - v.units[uid]))
            if p.earliest <= base.start_min <= p.latest_start:
                m.add_hint(v.start, base.start_min)

    def add_resource_constraints(self) -> None:
        m = self.model
        opt = self.opt
        lookups = ((self.room_ivs, opt._room_by_id, self.frozen_rooms), (self.staff_ivs, {}, None),
                   (self.unit_ivs, opt._unit_by_id, self.frozen_units), (self.bed_ivs, opt._bed_by_id, None))
        for groups, lookup, running in lookups:
            for key, intervals in groups.items():
                held = list(getattr(lookup.get(key), "blocked", ()))
                if running is not None:
                    # Planned closures / maintenance; a case already running when it was planned stays.
                    closed = [(a, b) for a, b, _ in lookup[key].closures]
                    held += [span for a, b in closed for span in _subtract([(a, b)], running[key])]
                fixed = [m.new_fixed_size_interval_var(a, b - a, f"held[{key},{a}]") for a, b in held]
                if len(intervals) + len(fixed) > 1:
                    m.add_no_overlap(intervals + fixed)
        for sid, items in self.week_load.items():
            person = self.opt._staff_by_id[sid]
            self._cap(f"week[{sid}]", items, max(0, person.remaining_minutes - self.frozen_load[sid]))
        w = self.weights
        for sid, items in self.day_load.items():
            person = self.opt._staff_by_id[sid]
            self._cap(f"day[{sid}]", items, max(0, person.daily_cap_minutes - self.frozen_day_load[sid]))
            # Spread the day: long days on one person cost more than the same work shared.
            base, most = self.frozen_day_load[sid], sum(d for d, _, _ in items)
            if w.day_spread_per_quarter and base + most > w.day_spread_after_mins:
                quarters = m.new_int_var(0, -(-(base + most - w.day_spread_after_mins) // 15), f"long_day[{sid}]")
                m.add(15 * quarters >= base + sum(d * lit for d, lit, _ in items) - w.day_spread_after_mins)
                self.objective.append(w.day_spread_per_quarter * quarters)
        # Rest: no (non-overriding) case time inside a rest period. Rest periods
        # may overlap each other, so: case time demand k, rest demand 1, capacity k.
        for sid, work in self.rest_work.items():
            person = self.opt._staff_by_id[sid]
            rests = list(self.rest_items.get(sid, []))
            if person.rest_until > 0 and person.rest_minutes > 0:
                rests.append(m.new_fixed_size_interval_var(
                    person.rest_until - person.rest_minutes, person.rest_minutes, f"rest_before[{sid}]"))
            if rests:
                k = len(rests)
                m.add_cumulative(work + rests, [k] * len(work) + [1] * k, k)

        for code, items in self.supply_load.items():
            item = opt._supply_by_id.get(code)
            if item is None:
                continue  # such cases are never planned (see _plan_case)
            frozen = self.frozen_supply[code]
            total = sum(q for q, _, _ in items)
            room = max(0, item.for_booked - frozen)  # what is on the shelf for this day
            if total > room:
                m.add(sum(q * lit for q, lit, _ in items) <= room)
            new = [(q, lit) for q, lit, is_new in items if is_new]
            if new and frozen + total > item.available:
                # New normal cases must not take what later bookings hold: either none of them uses
                # this item, or everything booked today fits in what is free for new cases.
                used = m.new_bool_var(f"new_uses[{code}]")
                m.add(sum(q * lit for q, lit in new) <= sum(q for q, _ in new) * used)
                m.add(frozen + sum(q * lit for q, lit, _ in items) <= item.available + (frozen + total) * (1 - used))

    def _cap(self, name: str, items, cap: int) -> None:
        """Sum of assigned minutes <= cap; emergency overrides may go over, at a cost."""
        if sum(d for d, _, _ in items) <= cap:
            return
        m = self.model
        load = sum(d * lit for d, lit, _ in items)
        flex = [(d, over) for d, _, over in items if over is not None]
        if not flex:
            m.add(load <= cap)
            return
        extra = m.new_int_var(0, sum(d for d, _ in flex), f"over_{name}")
        m.add(extra <= sum(d * over for d, over in flex))
        m.add(load <= cap + extra)
        self.objective.append(self.weights.fatigue_over_per_min * extra)

    def add_setup_grouping(self) -> None:
        """Per room, order cases with a circuit and charge for set-up switches."""
        m = self.model
        for or_id, members in self.room_members.items():
            if len(members) < 2:
                continue
            arcs = [(0, 0, m.new_bool_var(f"room_empty[{or_id}]"))]
            for i, (pi, xi, si, occ_i) in enumerate(members, 1):
                arcs.append((i, i, ~xi))
                arcs.append((0, i, m.new_bool_var(f"first[{or_id},{pi.case.case_id}]")))
                arcs.append((i, 0, m.new_bool_var(f"last[{or_id},{pi.case.case_id}]")))
                for j, (pj, xj, sj, _) in enumerate(members, 1):
                    if i == j or pi.earliest + occ_i > pj.latest_start:
                        continue  # j can never directly follow i
                    nxt = m.new_bool_var(f"next[{or_id},{pi.case.case_id}->{pj.case.case_id}]")
                    arcs.append((i, j, nxt))
                    m.add(sj >= si + occ_i).only_enforce_if(nxt)
                    cost = self.opt._setup_cost(pi.case, pj.case, self.weights)
                    if cost:
                        self.objective.append(cost * nxt)
            m.add_circuit(arcs)
