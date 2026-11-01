"""Domain model of the ORchestraAI scheduling engine.

Field names follow the Supabase schema in ``supabase/migrations`` so that the
data layer can map rows onto these classes directly. All times are integer
minutes from the start of the planning horizon (see ``timeutil``).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum


class Urgency(str, Enum):
    ELECTIVE = "ELECTIVE"
    URGENT = "URGENT"
    EMERGENCY = "EMERGENCY"


class InfectionRisk(str, Enum):
    CLEAN = "CLEAN"
    CONTAMINATED = "CONTAMINATED"
    DIRTY = "DIRTY"


class StaffRole:
    """Role names used in ``staff.role`` (free text in the DB)."""

    SURGEON = "SURGEON"
    ANESTHESIOLOGIST = "ANESTHESIOLOGIST"
    SCRUB_NURSE = "SCRUB_NURSE"
    CIRCULATING_NURSE = "CIRCULATING_NURSE"


class BedType:
    PACU = "PACU"
    ICU_SURGICAL = "ICU_SURGICAL"


DEFAULT_TEAM = {
    StaffRole.ANESTHESIOLOGIST: 1,
    StaffRole.SCRUB_NURSE: 1,
    StaffRole.CIRCULATING_NURSE: 1,
}

# How long a patient holds a recovery bed when the case does not say.
DEFAULT_RECOVERY_MINS = {BedType.PACU: 60, BedType.ICU_SURGICAL: 24 * 60}

UNUSABLE_ROOM_STATUSES = frozenset({"MAINTENANCE", "CLOSED", "OUT_OF_SERVICE"})
NEGATIVE_PRESSURE = "NEGATIVE_PRESSURE"  # room type / capability for airborne-infection cases
UNUSABLE_EQUIPMENT_STATUSES = frozenset({"MAINTENANCE"})
UNUSABLE_BED_STATUSES = frozenset({"CLOSED"})


def _closures(items) -> tuple[tuple[int, int, str], ...]:
    """Planned closed periods as sorted ``(start, end, reason)``."""
    out = []
    for c in items:
        a, b = int(c[0]), int(c[1])
        if b > a:
            out.append((a, b, str(c[2]) if len(c) > 2 and c[2] else ""))
    return tuple(sorted(out))


def _spans(spans) -> tuple[tuple[int, int], ...]:
    return tuple(sorted((int(a), int(b)) for a, b in spans if int(b) > int(a)))


@dataclass
class OperatingRoom:
    """Row of ``operating_rooms``.

    ``room_type`` also counts as a capability, so a procedure may require
    either ``"HYBRID"`` (a room type) or ``"ROBOTIC_READY"`` (a capability).
    ``closures`` are planned closed periods (``room_closures``: repair, deep
    cleaning): ``(start, end, reason)``; nothing may occupy the room then.
    ``elective_start`` / ``elective_end`` are the hours ELECTIVE cases may use
    (e.g. 08:00-17:00); URGENT and EMERGENCY cases may use the room any time
    it is open. ``None`` = no limit.
    """

    or_id: str
    room_type: str = "STANDARD"
    capabilities: frozenset[str] = frozenset()
    base_turnover_mins: int = 30
    infectious_turnover_mins: int = 60
    open_min: int = 0
    close_min: int | None = None  # None = open until the end of the horizon
    status: str = "AVAILABLE"
    blocked: tuple[tuple[int, int], ...] = ()  # held by bookings outside this horizon
    closures: tuple[tuple[int, int, str], ...] = ()
    elective_start: int | None = None
    elective_end: int | None = None

    def __post_init__(self) -> None:
        self.capabilities = frozenset(self.capabilities)
        self.blocked = _spans(self.blocked)
        self.closures = _closures(self.closures)

    @property
    def all_capabilities(self) -> frozenset[str]:
        return self.capabilities | {self.room_type}

    @property
    def is_usable(self) -> bool:
        return self.status not in UNUSABLE_ROOM_STATUSES

    def supports(self, required: frozenset[str]) -> bool:
        return required <= self.all_capabilities

    def closed_at(self, start: int, end: int) -> tuple[int, int, str] | None:
        """The planned closure overlapping ``[start, end)``, if any."""
        return next((c for c in self.closures if c[0] < end and start < c[1]), None)

    def turnover_for(self, case: "SurgeryCase") -> int:
        if case.infection_risk == InfectionRisk.DIRTY or case.airborne:
            return self.infectious_turnover_mins
        return self.base_turnover_mins

    def close(self, horizon_mins: int) -> int:
        return horizon_mins if self.close_min is None else self.close_min

    def hours_for(self, case: "SurgeryCase", horizon_mins: int) -> tuple[int, int]:
        """When ``case`` may run in this room: opening hours, narrowed to the
        elective hours for ELECTIVE cases."""
        start, end = self.open_min, self.close(horizon_mins)
        if case.urgency is Urgency.ELECTIVE:
            if self.elective_start is not None:
                start = max(start, self.elective_start)
            if self.elective_end is not None:
                end = min(end, self.elective_end)
        return start, end


@dataclass
class StaffMember:
    """Row of ``staff`` plus its available ``staff_schedules`` windows.

    ``shifts`` holds only the windows the person can work (REGULAR / ON_CALL
    with ``is_available``); LEAVE rows are left out by the data layer.

    Fatigue (see ``fatigue``): ``current_weekly_hours`` is operating-room time
    already worked in the 7-day window before this horizon; ``rest_until``
    is the earliest start after a late-night case before the horizon and
    ``next_booking_min`` the first booked start after it.
    """

    staff_id: str
    name: str
    role: str
    shifts: tuple[tuple[int, int], ...] = ()
    skills: frozenset[str] = frozenset()
    specialty: str | None = None
    max_weekly_hours: float = 48
    current_weekly_hours: float = 0
    is_active: bool = True
    max_daily_hours: float = 10
    rest_hours: float = 8
    rest_until: int = 0
    next_booking_min: int | None = None

    def __post_init__(self) -> None:
        self.skills = frozenset(self.skills)
        self.shifts = tuple(sorted((int(a), int(b)) for a, b in self.shifts))
        for start, end in self.shifts:
            if end <= start:
                raise ValueError(f"{self.staff_id}: shift end must be after start")
        if self.max_daily_hours <= 0 or self.max_weekly_hours <= 0 or self.rest_hours < 0:
            raise ValueError(f"{self.staff_id}: hour limits must be positive")

    @property
    def weekly_cap_minutes(self) -> int:
        return int(round(self.max_weekly_hours * 60))

    @property
    def weekly_used_minutes(self) -> int:
        return int(round(self.current_weekly_hours * 60))

    @property
    def daily_cap_minutes(self) -> int:
        return int(round(self.max_daily_hours * 60))

    @property
    def rest_minutes(self) -> int:
        return int(round(self.rest_hours * 60))

    @property
    def remaining_minutes(self) -> int:
        """Minutes left before hitting ``max_weekly_hours`` (fatigue guard)."""
        return max(0, self.weekly_cap_minutes - self.weekly_used_minutes)


@dataclass
class EquipmentUnit:
    """Row of ``equipment_units`` joined with its ``equipment_types`` row."""

    unit_id: str
    eq_type: str
    sterilization_mins: int = 120
    requires_sterilization: bool = True
    available_at: int = 0  # when it is back from CSSD / current use
    status: str = "AVAILABLE"
    name: str | None = None
    blocked: tuple[tuple[int, int], ...] = ()  # in use / in CSSD for bookings outside this horizon
    closures: tuple[tuple[int, int, str], ...] = ()  # planned maintenance (start, end, reason)

    def __post_init__(self) -> None:
        self.blocked = _spans(self.blocked)
        self.closures = _closures(self.closures)

    def closed_at(self, start: int, end: int) -> tuple[int, int, str] | None:
        """Planned maintenance overlapping ``[start, end)``, if any."""
        return next((c for c in self.closures if c[0] < end and start < c[1]), None)

    @property
    def reprocess_mins(self) -> int:
        """Time after a case before the unit can be used again."""
        return self.sterilization_mins if self.requires_sterilization else 0

    @property
    def is_usable(self) -> bool:
        return self.status not in UNUSABLE_EQUIPMENT_STATUSES


@dataclass
class SupplyItem:
    """A single-use supply or implant (``supply_items``) as seen by one planning day.

    ``available`` is how many NEW cases of this horizon may use: stock on hand
    plus deliveries due by then, minus what bookings on other days hold, taken
    at the tightest point from this day on (later bookings keep theirs).
    ``for_booked`` is what the cases already booked on this day may use: the
    same but counting only bookings on earlier days (earlier cases are served
    first). Defaults to ``available``.
    """

    item_id: str  # code, e.g. "KNEE-IMP-5"
    name: str
    available: int
    unit: str = "ชิ้น"
    category: str = "CONSUMABLE"  # IMPLANT / CONSUMABLE
    note: str | None = None  # e.g. next delivery, shown when stock is short
    reorder_level: int = 0  # warn when this few are left
    for_booked: int | None = None

    def __post_init__(self) -> None:
        self.available = max(0, int(self.available))
        self.for_booked = self.available if self.for_booked is None else max(self.available, int(self.for_booked))


@dataclass
class RecoveryBed:
    """Row of ``recovery_beds``."""

    bed_id: str
    bed_type: str = BedType.PACU
    available_from: int = 0  # occupied (e.g. by a ward patient) until then
    blocked: tuple[tuple[int, int], ...] = ()  # reserved by bookings outside this horizon
    status: str = "AVAILABLE"  # CLOSED = not in use

    def __post_init__(self) -> None:
        self.blocked = _spans(self.blocked)

    @property
    def is_usable(self) -> bool:
        return self.status not in UNUSABLE_BED_STATUSES


@dataclass
class SurgeryCase:
    """A surgery request after intake (``surgery_requests`` + preference card).

    ``required_equipment_types`` lists one entry per unit needed, so two trays
    of the same type are written twice. ``required_staff_skills`` maps a role
    to skills every person filling that role must have. ``airborne`` marks a
    patient with an airborne infection (e.g. TB, measles): the case needs a
    negative-pressure room and the long (infectious) room turnover.
    """

    case_id: str
    patient_hn: str
    procedure: str
    surgeon_id: str
    duration_mins: int
    urgency: Urgency = Urgency.ELECTIVE
    required_capabilities: frozenset[str] = frozenset()
    required_staff_roles: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_TEAM))
    required_staff_skills: dict[str, frozenset[str]] = field(default_factory=dict)
    required_equipment_types: tuple[str, ...] = ()
    infection_risk: InfectionRisk = InfectionRisk.CLEAN
    requires_icu_post_op: bool = False
    recovery_mins: int | None = None
    preferred_start: int | None = None
    release_min: int = 0  # earliest start (patient ready / arrival)
    deadline_min: int | None = None  # latest allowed start
    patient_name: str | None = None
    airborne: bool = False
    required_supplies: dict[str, int] = field(default_factory=dict)  # supply code -> quantity

    def __post_init__(self) -> None:
        if self.duration_mins <= 0:
            raise ValueError(f"{self.case_id}: duration_mins must be positive")
        self.urgency = Urgency(self.urgency)
        self.infection_risk = InfectionRisk(self.infection_risk)
        self.required_capabilities = frozenset(self.required_capabilities)
        if self.airborne:
            self.required_capabilities |= {NEGATIVE_PRESSURE}
        self.required_equipment_types = tuple(self.required_equipment_types)
        self.required_staff_skills = {
            role: frozenset(skills) for role, skills in self.required_staff_skills.items()
        }
        for role, count in self.required_staff_roles.items():
            if count < 0:
                raise ValueError(f"{self.case_id}: negative staff count for {role}")
        self.required_supplies = {str(k): int(v) for k, v in self.required_supplies.items() if int(v) > 0}

    @property
    def equipment_counts(self) -> Counter[str]:
        return Counter(self.required_equipment_types)

    @property
    def equipment_profile(self) -> frozenset[str]:
        return frozenset(self.required_equipment_types)

    @property
    def recovery_bed_type(self) -> str:
        return BedType.ICU_SURGICAL if self.requires_icu_post_op else BedType.PACU

    @property
    def recovery_duration(self) -> int:
        if self.recovery_mins is not None:
            return self.recovery_mins
        return DEFAULT_RECOVERY_MINS[self.recovery_bed_type]
