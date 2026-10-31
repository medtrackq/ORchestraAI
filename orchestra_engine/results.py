"""Solver output: assignments, reschedule diffs and a printable report."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from .models import SurgeryCase
from .timeutil import fmt


@dataclass(frozen=True)
class Assignment:
    """One booked case. Maps to ``scheduled_surgeries`` + ``resource_allocations``."""

    case_id: str
    or_id: str
    start_min: int
    end_min: int
    cleanup_end_min: int  # room ready for the next case
    staff_ids: tuple[str, ...]  # primary surgeon first
    equipment_unit_ids: tuple[str, ...] = ()
    equipment_ready_min: tuple[tuple[str, int], ...] = ()  # (unit_id, back from CSSD)
    bed_id: str | None = None
    recovery_start_min: int | None = None
    recovery_end_min: int | None = None
    frozen: bool = False  # already started when rescheduled; kept as is
    reasoning: tuple[str, ...] = ()


@dataclass
class ScheduleChange:
    case_id: str
    details: list[str]
    before: Assignment | None
    after: Assignment | None


@dataclass
class Notification:
    staff_id: str
    case_id: str
    message: str


@dataclass
class ScheduleResult:
    scenario: str
    status: str  # OPTIMAL / FEASIBLE / INFEASIBLE / UNKNOWN / MODEL_INVALID
    objective: float | None
    assignments: dict[str, Assignment]
    unscheduled: dict[str, list[str]]  # case_id -> reasons
    cases: dict[str, SurgeryCase]
    staff_names: dict[str, str]
    wall_time_s: float
    now_min: int = 0
    changes: list[ScheduleChange] = field(default_factory=list)
    notifications: list[Notification] = field(default_factory=list)

    @property
    def is_feasible(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")

    def by_room(self) -> dict[str, list[Assignment]]:
        rooms: dict[str, list[Assignment]] = defaultdict(list)
        for a in self.assignments.values():
            rooms[a.or_id].append(a)
        return {r: sorted(v, key=lambda a: a.start_min) for r, v in sorted(rooms.items())}

    def _name(self, staff_id: str) -> str:
        return self.staff_names.get(staff_id, staff_id)

    def report(self) -> str:
        total = len(self.cases)
        obj = "-" if self.objective is None else f"{self.objective:.0f}"
        lines = [
            f"=== {self.scenario} ===",
            f"status={self.status}  scheduled={len(self.assignments)}/{total}  "
            f"objective={obj}  solve={self.wall_time_s:.2f}s"
            + (f"  now={fmt(self.now_min)}" if self.now_min else ""),
        ]
        for room, items in self.by_room().items():
            lines.append(f"[{room}]")
            for a in items:
                case = self.cases[a.case_id]
                tag = " (in progress, locked)" if a.frozen else ""
                lines.append(
                    f"  {fmt(a.start_min)}-{fmt(a.end_min)} clean->{fmt(a.cleanup_end_min)}  "
                    f"{a.case_id} {case.urgency.value:<9} {case.procedure}{tag}"
                )
                lines.append("      team: " + ", ".join(self._name(s) for s in a.staff_ids))
                if a.equipment_unit_ids:
                    ready = ", ".join(f"{u} (CSSD->{fmt(t)})" for u, t in a.equipment_ready_min)
                    lines.append(f"      equipment: {ready}")
                if a.bed_id is not None:
                    lines.append(
                        f"      recovery: {a.bed_id} {fmt(a.recovery_start_min)}-{fmt(a.recovery_end_min)}"
                    )
                for why in a.reasoning:
                    lines.append(f"      why: {why}")
        if self.unscheduled:
            lines.append("Not scheduled:")
            for cid, reasons in self.unscheduled.items():
                case = self.cases[cid]
                lines.append(f"  {cid} {case.urgency.value} {case.procedure}")
                lines.extend(f"      - {r}" for r in reasons)
        if self.changes:
            lines.append("Changes vs previous schedule:")
            for ch in self.changes:
                lines.append(f"  {ch.case_id}: " + "; ".join(ch.details))
        if self.notifications:
            people = sorted({self._name(n.staff_id) for n in self.notifications})
            lines.append(f"Notifications: {len(self.notifications)} messages to {len(people)} staff")
            lines.append("  " + ", ".join(people))
        return "\n".join(lines)
