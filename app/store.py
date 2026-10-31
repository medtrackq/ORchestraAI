"""In-memory schedule for the web app (synthetic demo hospital, one planning day).

Every change goes through the engine and is checked by ``validate_schedule``
before it replaces the current schedule, so the stored schedule never holds
a conflict. State lives in memory: it resets when the service restarts. The
Supabase data layer will replace this class.
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field

from orchestra_engine import (
    ScheduleResult,
    StaffRole,
    SurgeryCase,
    Urgency,
    fmt,
    hm,
    validate_schedule,
)
from orchestra_engine.demo import Hospital, build_hospital, elective_day


class StoreError(ValueError):
    """Request that cannot be carried out; the message is shown to the user."""


@dataclass
class Activity:
    kind: str  # reset / booking / emergency / overrun
    title: str
    log: list[str] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    notifications: list[dict] = field(default_factory=list)
    highlight: list[str] = field(default_factory=list)


def parse_time(text: str) -> int:
    try:
        minute = hm(text)
    except (ValueError, AttributeError) as exc:
        raise StoreError(f"รูปแบบเวลาไม่ถูกต้อง: {text!r} (ใช้ HH:MM)") from exc
    if not 0 <= minute < 24 * 60:
        raise StoreError(f"เวลาต้องอยู่ระหว่าง 00:00-23:59: {text}")
    return minute


class ScheduleStore:
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
            "rooms": [{"id": r.or_id, "type": r.room_type, "caps": sorted(r.capabilities)} for r in h.rooms],
            "surgeons": [
                {"id": s.staff_id, "name": s.name,
                 "shifts": [f"{fmt(a)}-{fmt(b)}" for a, b in s.shifts]}
                for s in h.staff if s.role == StaffRole.SURGEON and s.is_active
            ],
            "equipment_types": [{"type": t, "units": n} for t, n in sorted(usable.items())],
            "capabilities": caps,
            "bed_types": sorted({b.bed_type for b in h.beds}),
        }

    def state(self) -> dict:
        schedule = self.result.to_dict()
        bookings = schedule["bookings"]
        start = min([hm("08:00"), *[b["start_min"] for b in bookings]]) // 60 * 60
        end = -(-max([hm("18:00"), *[b["cleanup_end_min"] for b in bookings]]) // 60) * 60
        return {
            "planning_day": "วันตัวอย่าง (ข้อมูลจำลอง)",
            "schedule": schedule,
            "axis": {"start": start, "end": end},
            "options": self.options(),
            "activity": [a.__dict__ for a in reversed(self.activity[-30:])],
        }

    # ------------------------------------------------------------- writes

    def _next_id(self, prefix: str, commit: bool) -> str:
        number = self._ids[prefix] + 1
        if commit:
            self._ids[prefix] = number
        return f"{prefix}{number:03d}"

    def _build_case(self, data: dict, case_id: str, *, release: int = 0) -> SurgeryCase:
        h = self.hospital
        surgeon = next((s for s in h.staff if s.staff_id == data["surgeon_id"]), None)
        if surgeon is None or surgeon.role != StaffRole.SURGEON:
            raise StoreError(f"ไม่พบศัลยแพทย์ {data['surgeon_id']}")
        known_types = {u.eq_type for u in h.equipments}
        unknown = [t for t in data.get("required_equipment_types", []) if t not in known_types]
        if unknown:
            raise StoreError(f"ไม่รู้จักเครื่องมือ: {', '.join(unknown)}")
        known_caps = {c for r in h.rooms for c in r.all_capabilities}
        unknown = [c for c in data.get("required_capabilities", []) if c not in known_caps]
        if unknown:
            raise StoreError(f"ไม่มีห้องที่มีความสามารถ: {', '.join(unknown)}")
        skills = {StaffRole.SCRUB_NURSE: {"ROBOTIC_CERTIFIED"}} if data.get("robotic_certified_scrub") else {}
        return SurgeryCase(
            case_id, data["patient_hn"], data["procedure"], surgeon.staff_id, int(data["duration_mins"]),
            urgency=Urgency(data.get("urgency", "ELECTIVE")),
            required_capabilities=set(data.get("required_capabilities", [])),
            required_staff_skills=skills,
            required_equipment_types=list(data.get("required_equipment_types", [])),
            infection_risk=data.get("infection_risk", "CLEAN"),
            requires_icu_post_op=bool(data.get("requires_icu_post_op")),
            release_min=release,
        )

    def _commit(self, result: ScheduleResult) -> None:
        h = self.hospital
        errors = validate_schedule(result, result.cases.values(), h.rooms, h.equipments, h.staff, h.beds)
        if errors:  # never store a schedule with a conflict
            raise StoreError("ตารางที่คำนวณได้ไม่ผ่านการตรวจ: " + "; ".join(errors[:3]))
        self.result = result

    def slot_check(self, data: dict, start_text: str, *, commit: bool) -> dict:
        start = parse_time(start_text)
        with self._lock:
            if start < self.result.now_min:
                raise StoreError(f"เวลาที่ขอผ่านไปแล้ว (เวลาปัจจุบันของตาราง {fmt(self.result.now_min)})")
            case = self._build_case(data, self._next_id("R", commit=False))
            check = self.opt.check_slot(self.result, case, start, verbose=False)
            out = check.to_dict()
            out["booked"] = False
            if commit and check.assignment is not None:
                self._commit(check.result)
                self._next_id("R", commit=True)
                out["booked"] = True
                self.activity.append(Activity(
                    "booking", f"จอง {case.case_id} {case.procedure}", check.log,
                    [{"case_id": c.case_id, "details": c.details} for c in check.result.changes],
                    check.result.to_dict()["notifications"], [case.case_id],
                ))
            return out

    def emergency(self, data: dict, now_text: str) -> dict:
        now = parse_time(now_text)
        with self._lock:
            case = self._build_case({**data, "urgency": "EMERGENCY"}, self._next_id("E", commit=False), release=now)
            after = self.opt.insert_emergency(
                self.result, list(self.result.cases.values()), [case], now_min=now,
                scenario_name=f"Emergency {case.case_id}", verbose=False,
            )
            if not after.is_feasible:
                raise StoreError(f"คำนวณไม่สำเร็จ ({after.status})")
            self._commit(after)
            self._next_id("E", commit=True)
            return self._record_change("emergency", f"ฉุกเฉิน {case.case_id} {case.procedure}", after, case.case_id)

    def overrun(self, case_id: str, new_duration: int, now_text: str) -> dict:
        now = parse_time(now_text)
        with self._lock:
            current = self.result.assignments.get(case_id)
            if current is None:
                raise StoreError(f"ไม่พบเคส {case_id} ในตาราง")
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
            self._commit(after)
            return self._record_change("overrun", f"{case_id} ผ่าตัดเกินเวลา -> {new_duration} นาที", after, case_id)

    def _record_change(self, kind: str, title: str, after: ScheduleResult, case_id: str) -> dict:
        data = after.to_dict()
        log = [f"[Master Orchestrator] {c['case_id']}: {'; '.join(c['details'])}" for c in data["changes"]]
        unscheduled = [f"[Master Orchestrator] NOT SCHEDULED {u['case_id']}: {'; '.join(u['reasons'])}"
                       for u in data["unscheduled"]]
        self.activity.append(Activity(
            kind, title, log + unscheduled, data["changes"], data["notifications"],
            [c["case_id"] for c in data["changes"]] or [case_id],
        ))
        return {"changes": data["changes"], "notifications": data["notifications"],
                "unscheduled": data["unscheduled"], "log": log + unscheduled}
