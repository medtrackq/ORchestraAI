"""In-memory schedule for the web app (synthetic demo hospital, one planning day).

Every change goes through the engine and is checked by ``validate_schedule``
before it replaces the current schedule, so the stored schedule never holds
a conflict. State lives in memory: it resets when the service restarts. The
Supabase data layer will replace this class.
"""
from __future__ import annotations

import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from orchestra_engine import (
    ScheduleResult,
    StaffRole,
    SurgeryCase,
    Urgency,
    fatigue_issues,
    fmt,
    hm,
    staff_fatigue,
)
from orchestra_engine.demo import Hospital, build_hospital, elective_day
from orchestra_engine.models import DEFAULT_TEAM


class StoreError(ValueError):
    """Request that cannot be carried out; the message is shown to the user."""


class Forbidden(StoreError):
    """The signed-in role may not do this (HTTP 403)."""


EQUIPMENT_STATUSES = ("AVAILABLE", "IN_USE", "STERILIZING", "MAINTENANCE")


def actor_name(actor: dict | None) -> str | None:
    return None if actor is None else f"{actor.get('name')} ({actor.get('role')})"


def check_team(actor: dict | None, staff_ids) -> None:
    """Surgeons and clinical staff may record times only for cases they are on."""
    if actor and actor.get("role") in ("surgeon", "staff") and actor.get("staff_id") not in staff_ids:
        raise Forbidden("บันทึกเวลาได้เฉพาะเคสที่คุณอยู่ในทีม")


def check_own_case(actor: dict | None, surgeon_id: str) -> None:
    """Surgeons may only act on cases where they are the primary surgeon."""
    if actor and actor.get("role") == "surgeon" and actor.get("staff_id") != surgeon_id:
        raise Forbidden("ศัลยแพทย์ทำรายการได้เฉพาะเคสที่ตัวเองเป็นแพทย์ผ่าตัดหลัก")


def equipment_rows(units) -> list[dict]:
    return [
        {"unit_id": u.unit_id, "type": u.eq_type, "status": u.status,
         "ready_at": fmt(u.available_at) if 0 < u.available_at < 24 * 60 else None,
         "sterilization_mins": u.reprocess_mins, "usable": u.is_usable}
        for u in sorted(units, key=lambda u: (u.eq_type, u.unit_id))
    ]


def ready_minute(status: str, ready_text: str | None) -> int | None:
    if status not in EQUIPMENT_STATUSES:
        raise StoreError(f"สถานะไม่ถูกต้อง: {status}")
    if status in ("IN_USE", "STERILIZING") and not ready_text:
        raise StoreError("กรุณาระบุเวลาที่เครื่องมือจะพร้อมใช้")
    return parse_time(ready_text) if ready_text else None


@dataclass
class Activity:
    kind: str  # reset / booking / emergency / overrun
    title: str
    log: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    notifications: list[dict] = field(default_factory=list)
    highlight: list[str] = field(default_factory=list)
    actor: str | None = None


KNOWN_ROLES = {StaffRole.ANESTHESIOLOGIST, StaffRole.SCRUB_NURSE, StaffRole.CIRCULATING_NURSE, StaffRole.SURGEON}
SKILL = re.compile(r"^[A-Z][A-Z0-9_]{1,49}$")


def room_options(hospital: Hospital) -> list[dict]:
    """Rooms for the schedule view: type, capabilities, status and planned closures."""
    return [
        {"id": r.or_id, "type": r.room_type, "caps": sorted(r.capabilities), "status": r.status,
         "usable": r.is_usable, "closures": [{"start_min": a, "end_min": b, "reason": why} for a, b, why in r.closures]}
        for r in hospital.rooms
    ]


def supply_options(hospital: Hospital, result: ScheduleResult) -> list[dict]:
    """Stock of each supply for this day: what is free for new cases, and low-stock flags."""
    booked: Counter[str] = Counter()
    for cid in result.assignments:
        booked.update(result.cases[cid].required_supplies)
    return [
        {"id": s.item_id, "name": s.name, "unit": s.unit, "category": s.category, "available": s.available,
         "booked": booked[s.item_id], "left": max(0, s.available - booked[s.item_id]),
         "low": s.available - booked[s.item_id] <= s.reorder_level, "note": s.note}
        for s in hospital.supplies
    ]


def staff_skills(hospital: Hospital) -> dict[str, list[str]]:
    """Skills held by active staff, per role (what a booking can ask for)."""
    out: dict[str, set[str]] = {}
    for s in hospital.staff:
        if s.is_active and s.skills:
            out.setdefault(s.role, set()).update(s.skills)
    return {role: sorted(skills) for role, skills in sorted(out.items())}


def required_skills(data: dict) -> dict[str, set[str]]:
    skills: dict[str, set[str]] = {}
    for role, names in (data.get("required_staff_skills") or {}).items():
        if role not in KNOWN_ROLES:
            raise StoreError(f"บทบาทไม่ถูกต้องในทักษะที่ต้องการ: {role}")
        bad = [n for n in names if not SKILL.match(str(n))]
        if bad:
            raise StoreError(f"ชื่อทักษะไม่ถูกต้อง: {', '.join(map(str, bad))}")
        if names:
            skills[role] = set(names)
    if data.get("robotic_certified_scrub"):
        skills.setdefault(StaffRole.SCRUB_NURSE, set()).add("ROBOTIC_CERTIFIED")
    return skills


def fatigue_view(hospital: Hospital, result: ScheduleResult) -> dict:
    """Staffing Agent view: every person's load and fatigue score, and the
    fatigue-limit breaks of the day (emergency overrides and history)."""
    issues = fatigue_issues(result.assignments, result.cases, hospital.staff)
    return {
        "staff": staff_fatigue(hospital.staff, result.assignments, issues, now_min=result.now_min),
        "fatigue": [i.to_dict() for i in issues],
    }


def fatigue_log(issues) -> list[str]:
    return [f"[Staffing Agent] FATIGUE OVERRIDE: {i.detail()}" for i in issues if i.override]


def build_case(hospital: Hospital, data: dict, case_id: str, *, release: int = 0) -> SurgeryCase:
    """Turn a request from the web form into a ``SurgeryCase`` after checking it."""
    surgeon = next((s for s in hospital.staff if s.staff_id == data["surgeon_id"]), None)
    if surgeon is None or surgeon.role != StaffRole.SURGEON:
        raise StoreError(f"ไม่พบศัลยแพทย์ {data['surgeon_id']}")
    known_types = {u.eq_type for u in hospital.equipments}
    unknown = [t for t in data.get("required_equipment_types") or [] if t not in known_types]
    if unknown:
        raise StoreError(f"ไม่รู้จักเครื่องมือ: {', '.join(unknown)}")
    known_caps = {c for r in hospital.rooms for c in r.all_capabilities}
    unknown = [c for c in data.get("required_capabilities") or [] if c not in known_caps]
    if unknown:
        raise StoreError(f"ไม่มีห้องที่มีความสามารถ: {', '.join(unknown)}")
    roles = data.get("required_staff_roles") or dict(DEFAULT_TEAM)
    bad = [r for r, n in roles.items() if r not in KNOWN_ROLES or not 0 <= int(n) <= 4]
    if bad:
        raise StoreError(f"บทบาททีมไม่ถูกต้อง: {', '.join(bad)}")
    skills = required_skills(data)
    stocked = {s.item_id for s in hospital.supplies}
    supplies: dict[str, int] = {}
    for code, qty in (data.get("required_supplies") or {}).items():
        if code not in stocked:
            raise StoreError(f"ไม่รู้จักวัสดุ/รากเทียม: {code}")
        if not 0 <= int(qty) <= 50:
            raise StoreError(f"จำนวน {code} ต้องอยู่ระหว่าง 0-50")
        supplies[code] = int(qty)
    return SurgeryCase(
        case_id, data["patient_hn"], data["procedure"], surgeon.staff_id, int(data["duration_mins"]),
        urgency=Urgency(data.get("urgency") or "ELECTIVE"),
        required_capabilities=set(data.get("required_capabilities") or []),
        required_staff_roles={r: int(n) for r, n in roles.items()},
        required_staff_skills=skills,
        required_equipment_types=list(data.get("required_equipment_types") or []),
        infection_risk=data.get("infection_risk") or "CLEAN",
        requires_icu_post_op=bool(data.get("requires_icu_post_op")),
        airborne=bool(data.get("airborne")),
        required_supplies=supplies,
        release_min=release,
        patient_name=(data.get("patient_name") or "").strip() or None,
    )


def parse_time(text: str) -> int:
    try:
        minute = hm(text)
    except (ValueError, AttributeError) as exc:
        raise StoreError(f"รูปแบบเวลาไม่ถูกต้อง: {text!r} (ใช้ HH:MM)") from exc
    if not 0 <= minute < 24 * 60:
        raise StoreError(f"เวลาต้องอยู่ระหว่าง 00:00-23:59: {text}")
    return minute


class ScheduleStore:
    """Demo mode: synthetic hospital kept in memory (used when DATABASE_URL is not set)."""

    mode = "demo"

    def __init__(self, *, workers: int = 2, time_limit_s: float = 10.0) -> None:
        self._lock = threading.Lock()
        self.workers = workers
        self.time_limit_s = time_limit_s
        self.reset()

    # ----------------------------------------------------------- lifecycle

    def reset(self) -> None:
        with self._lock:
            self.hospital: Hospital = build_hospital()
            self.opt = self.hospital.optimizer(time_limit_s=self.time_limit_s, num_workers=self.workers)
            self.result: ScheduleResult = self.opt.solve_schedule(elective_day(), "Elective day", verbose=False)
            from orchestra_engine.intake import HistoryRecord, synthetic_history

            from .seed import demo_catalog

            self.procedures, self.cards = demo_catalog()
            self.history = [HistoryRecord(r["surgeon_id"], r["procedure_id"], r["actual"])
                            for r in synthetic_history(self.cards)]
            self.actuals: dict[str, list[int | None]] = {}  # case_id -> [start, end] minutes
            self.pending: dict[str, SurgeryCase] = {}  # taken off the schedule, waiting for a new slot
            self._ids = Counter()
            self.activity: list[Activity] = [Activity(
                "reset", "โหลดข้อมูลตัวอย่าง",
                [f"[Master Orchestrator] booked {len(self.result.assignments)} elective cases, "
                 f"status {self.result.status}"],
            )]

    # -------------------------------------------------------------- reads

    def options(self) -> dict:
        h = self.hospital
        usable = Counter(u.eq_type for u in h.equipments if u.is_usable)
        caps = sorted({c for r in h.rooms for c in r.all_capabilities})
        return {
            "rooms": room_options(h),
            "surgeons": [
                {"id": s.staff_id, "name": s.name,
                 "shifts": [f"{fmt(a)}-{fmt(b)}" for a, b in s.shifts]}
                for s in h.staff if s.role == StaffRole.SURGEON and s.is_active
            ],
            "equipment_types": [{"type": t, "units": n} for t, n in sorted(usable.items())],
            "capabilities": caps,
            "bed_types": sorted({b.bed_type for b in h.beds}),
            "procedures": self.procedures,
            "cards": self.cards,
            "skills": staff_skills(h),
            "supplies": supply_options(h, self.result),
        }

    def state(self, day: str | None = None) -> dict:
        schedule = self.result.to_dict()
        bookings = schedule["bookings"]
        for b in bookings:
            start, end = self.actuals.get(b["case_id"], [None, None])
            b["actual_start"] = fmt(start) if start is not None else None
            b["actual_end"] = fmt(end) if end is not None else None
        start = min([hm("08:00"), *[b["start_min"] for b in bookings]]) // 60 * 60
        now = self.result.now_min
        end = -(-max([hm("18:00"), *[b["cleanup_end_min"] for b in bookings],
                      *([now + 30] if 0 < now < 24 * 60 else [])]) // 60) * 60
        return {
            "mode": self.mode,
            "date": None,
            "planning_day": "วันตัวอย่าง (ข้อมูลจำลอง)",
            "warnings": [],
            "rls_off": [],
            "empty": False,
            "schedule": schedule,
            "axis": {"start": start, "end": end},
            "options": self.options(),
            "activity": [a.__dict__ for a in reversed(self.activity[-30:])],
            "pending": self.pending_rows(),
            **fatigue_view(self.hospital, self.result),
        }

    # ------------------------------------------------------------- writes

    def _next_id(self, prefix: str, commit: bool) -> str:
        number = self._ids[prefix] + 1
        if commit:
            self._ids[prefix] = number
        return f"{prefix}{number:03d}"

    def _commit(self, result: ScheduleResult) -> None:
        h = self.hospital
        errors = h.validate(result)
        if errors:  # never store a schedule with a conflict
            raise StoreError("ตารางที่คำนวณได้ไม่ผ่านการตรวจ: " + "; ".join(errors[:3]))
        for cid in set(self.result.assignments) - set(result.assignments):
            self.pending[cid] = self.result.cases[cid]  # keep it visible until it is rebooked or cancelled
        for cid in result.assignments:
            self.pending.pop(cid, None)
        self.result = result

    def _case_id(self, data: dict, prefix: str, commit: bool) -> str:
        """Rebooking a waiting case keeps its id."""
        pending = data.get("pending_id")
        if pending:
            if pending not in self.pending:
                raise StoreError("คำขอนี้ถูกจองหรือยกเลิกไปแล้ว กรุณาโหลดใหม่")
            return pending
        return self._next_id(prefix, commit=commit)

    def pending_rows(self) -> list[dict]:
        by_name = {p["name"]: p["id"] for p in self.procedures}
        names = {s.staff_id: s.name for s in self.hospital.staff}
        rank = {Urgency.EMERGENCY: 0, Urgency.URGENT: 1, Urgency.ELECTIVE: 2}
        return [
            {"id": cid, "code": cid, "patient_hn": c.patient_hn, "patient_name": c.patient_name,
             "surgeon_id": c.surgeon_id, "surgeon": names.get(c.surgeon_id), "procedure_id": by_name.get(c.procedure),
             "procedure": c.procedure, "urgency": c.urgency.value, "duration_mins": c.duration_mins,
             "airborne": c.airborne, "was_on": None, "supplies": dict(c.required_supplies)}
            for cid, c in sorted(self.pending.items(), key=lambda kv: (rank[kv[1].urgency], kv[0]))
        ]

    def cancel_pending(self, pending_id: str, *, actor: dict | None = None) -> dict:
        with self._lock:
            case = self.pending.get(pending_id)
            if case is None:
                raise StoreError("คำขอนี้ถูกจองหรือยกเลิกไปแล้ว")
            check_own_case(actor, case.surgeon_id)
            del self.pending[pending_id]
            self.activity.append(Activity("cancel", f"ยกเลิกคำขอที่รอจัด {pending_id}", [], actor=actor_name(actor)))
        return {"cancelled": pending_id}

    # ------------------------------------------------------------- intake

    def intake_context(self, day: str | None = None):
        from orchestra_engine.intake import HistoryRecord

        from .intake_service import IntakeContext

        by_name = {p["name"]: p["id"] for p in self.procedures}
        recorded = []
        for cid, (start, end) in self.actuals.items():
            case, a = self.result.cases.get(cid), self.result.assignments.get(cid)
            if case and a and start is not None and end is not None and case.procedure in by_name:
                recorded.append(HistoryRecord(a.staff_ids[0], by_name[case.procedure], end - start))
        surgeons = [{"id": s.staff_id, "name": s.name} for s in self.hospital.staff
                    if s.role == StaffRole.SURGEON and s.is_active]
        return IntakeContext(date.today(), self.procedures, surgeons, self.cards, self.history + recorded)

    def record_actual(self, case_id: str, start_text: str | None, end_text: str | None, *,
                      day: str | None = None, actor: dict | None = None) -> dict:
        if not start_text and not end_text:
            raise StoreError("กรุณาใส่เวลาเริ่มหรือเวลาเสร็จ")
        with self._lock:
            a = self.result.assignments.get(case_id)
            if a is None:
                raise StoreError(f"ไม่พบเคส {case_id} ในตาราง")
            check_team(actor, a.staff_ids)
            times = self.actuals.setdefault(case_id, [None, None])
            if start_text:
                times[0] = parse_time(start_text)
            if end_text:
                times[1] = parse_time(end_text)
            if times[1] is not None and times[0] is None:
                raise StoreError("บันทึกเวลาเริ่มผ่าตัดก่อน")
            if times[1] is not None and times[1] <= times[0]:
                times[1] += 24 * 60  # finished after midnight
            planned = a.end_min - a.start_min
            minutes = times[1] - times[0] if times[1] is not None else None
            title = f"{case_id} เวลาจริง " + (f"{minutes} นาที (วางแผน {planned})" if minutes else
                                               f"เริ่ม {fmt(times[0])}")
            self.activity.append(Activity("actual", title, [], actor=actor_name(actor)))
            return {"case_id": case_id, "actual_minutes": minutes, "planned_minutes": planned}

    def people(self) -> list[dict]:
        return [{"id": s.staff_id, "name": s.name, "role": s.role} for s in self.hospital.staff]

    def equipment(self, day: str | None = None) -> list[dict]:
        return equipment_rows(self.hospital.equipments)

    def update_equipment(self, serial: str, status: str, ready_text: str | None, *,
                         day: str | None = None, actor: dict | None = None) -> dict:
        ready = ready_minute(status, ready_text)
        with self._lock:
            unit = next((u for u in self.hospital.equipments if u.unit_id == serial), None)
            if unit is None:
                raise StoreError(f"ไม่พบเครื่องมือ {serial}")
            unit.status, unit.available_at = status, ready or 0
            h = self.hospital
            problems = [p for p in h.validate(self.result) if serial in p]
            when = f" พร้อม {fmt(ready)}" if ready else ""
            self.activity.append(Activity("equipment", f"{serial}: {status}{when}", problems,
                                          actor=actor_name(actor)))
            return {"unit": equipment_rows([unit])[0], "warnings": problems}

    def slot_check(self, data: dict, start_text: str, *, commit: bool, day: str | None = None,
                   actor: dict | None = None) -> dict:
        start = parse_time(start_text)
        with self._lock:
            if start < self.result.now_min:
                raise StoreError(f"เวลาที่ขอผ่านไปแล้ว (เวลาปัจจุบันของตาราง {fmt(self.result.now_min)})")
            case = build_case(self.hospital, data, self._case_id(data, "R", commit=False))
            check = self.opt.check_slot(self.result, case, start, verbose=False)
            out = check.to_dict()
            out["booked"] = False
            if commit and check.assignment is not None:
                self._commit(check.result)
                if not data.get("pending_id"):
                    self._next_id("R", commit=True)
                out["booked"] = True
                self.activity.append(Activity(
                    "booking", f"จอง {case.case_id} {case.procedure}", check.log,
                    [{"case_id": c.case_id, "details": c.details} for c in check.result.changes],
                    check.result.to_dict()["notifications"], [case.case_id], actor_name(actor),
                ))
            return out

    def emergency(self, data: dict, now_text: str, *, day: str | None = None, actor: dict | None = None) -> dict:
        now = parse_time(now_text)
        with self._lock:
            case = build_case(self.hospital, {**data, "urgency": "EMERGENCY"}, self._case_id(data, "E", commit=False),
                              release=now)
            after = self.opt.insert_emergency(
                self.result, list(self.result.cases.values()), [case], now_min=now,
                scenario_name=f"Emergency {case.case_id}", verbose=False,
            )
            if not after.is_feasible:
                raise StoreError(f"คำนวณไม่สำเร็จ ({after.status})")
            tired = self.opt.new_fatigue(self.result, after)
            self._commit(after)
            if not data.get("pending_id"):
                self._next_id("E", commit=True)
            return self._record_change("emergency", f"ฉุกเฉิน {case.case_id} {case.procedure}", after, case.case_id,
                                       actor, tired)

    def overrun(self, case_id: str, new_duration: int, now_text: str, *, day: str | None = None,
                actor: dict | None = None) -> dict:
        now = parse_time(now_text)
        with self._lock:
            current = self.result.assignments.get(case_id)
            if current is None:
                raise StoreError(f"ไม่พบเคส {case_id} ในตาราง")
            check_own_case(actor, current.staff_ids[0])
            if not current.start_min < now:
                raise StoreError(f"{case_id} ยังไม่เริ่ม (เริ่ม {fmt(current.start_min)}) ให้แก้เวลาผ่าตัดแทน")
            if current.start_min + new_duration <= now:
                raise StoreError("เวลาผ่าตัดใหม่ต้องจบหลังเวลาปัจจุบัน")
            after = self.opt.handle_overrun(
                self.result, list(self.result.cases.values()), case_id,
                new_duration_mins=new_duration, now_min=now, scenario_name=f"Overrun {case_id}", verbose=False,
            )
            if not after.is_feasible:
                raise StoreError(f"คำนวณไม่สำเร็จ ({after.status})")
            tired = self.opt.new_fatigue(self.result, after)
            self._commit(after)
            return self._record_change("overrun", f"{case_id} ผ่าตัดเกินเวลา -> {new_duration} นาที", after, case_id,
                                       actor, tired)

    def replan(self, day: str | None = None, *, actor: dict | None = None) -> dict:
        """Re-plan the cases that have not started, changing as little as possible."""
        with self._lock:
            after = self.opt.reschedule(self.result, list(self.result.cases.values()), now_min=self.result.now_min,
                                        scenario_name="Replan", verbose=False)
            if not after.is_feasible:
                raise StoreError(f"คำนวณไม่สำเร็จ ({after.status})")
            tired = self.opt.new_fatigue(self.result, after)
            self._commit(after)
            return self._record_change("replan", "จัดตารางวันนี้ใหม่", after, "", actor, tired)

    def _record_change(self, kind: str, title: str, after: ScheduleResult, case_id: str,
                       actor: dict | None = None, tired=()) -> dict:
        data = after.to_dict()
        log = [f"[Master Orchestrator] {c['case_id']}: {'; '.join(c['details'])}" for c in data["changes"]]
        log += [f"[Master Orchestrator] NOT SCHEDULED {u['case_id']}: {'; '.join(u['reasons'])}"
                for u in data["unscheduled"]]
        log += fatigue_log(tired)
        self.activity.append(Activity(
            kind, title, log, data["changes"], data["notifications"],
            [c["case_id"] for c in data["changes"]] or [case_id], actor_name(actor),
        ))
        return {"changes": data["changes"], "notifications": data["notifications"],
                "unscheduled": data["unscheduled"], "log": log, "fatigue": [i.to_dict() for i in tired]}
