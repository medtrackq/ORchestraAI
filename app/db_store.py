"""Supabase mode: every request reads the planning day from the database.

Writes run in one transaction under an advisory lock (one scheduler write at
a time); the engine result is checked with ``validate_schedule`` and must not
add any conflict before it is saved. The exclusion constraints in the schema
are the final guard.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import date

import psycopg

from datetime import timedelta

from orchestra_engine import ORchestraOptimizer, ScheduleResult, fmt, hm, validate_schedule
from orchestra_engine.demo import elective_day

from . import seed
from .db import ConflictError, Database, DatabaseUnavailable, World, case_code
from .intake_service import IntakeContext
from .store import (Activity, StoreError, actor_name, build_case, check_own_case, check_team, equipment_rows,
                    fatigue_log, fatigue_view, parse_time, ready_minute, staff_skills)


class DatabaseStore:
    mode = "supabase"

    def __init__(self, url: str, *, tz: str = "Asia/Bangkok", workers: int = 2, time_limit_s: float = 10.0) -> None:
        self.db = Database(url, tz)
        self.workers = workers
        self.time_limit_s = time_limit_s
        self._fallback_activity: dict[date, list[Activity]] = {}  # if the activity table is missing

    # ------------------------------------------------------------- helpers

    @contextmanager
    def _transaction(self, *, write: bool):
        try:
            with self.db.connect() as conn:
                if write:
                    self.db.lock(conn)
                yield conn
        except DatabaseUnavailable as exc:
            raise StoreError(f"เชื่อมต่อฐานข้อมูลไม่ได้: {exc}") from exc
        except ConflictError as exc:
            raise StoreError(f"ตารางในฐานข้อมูลเปลี่ยนระหว่างคำนวณ กรุณาลองใหม่ ({exc})") from exc
        except psycopg.OperationalError as exc:
            raise StoreError(f"เชื่อมต่อฐานข้อมูลไม่ได้: {str(exc).splitlines()[0]}") from exc
        except psycopg.errors.UndefinedTable as exc:
            raise StoreError(f"ไม่พบตารางในฐานข้อมูล (รัน migration แล้วหรือยัง?): {exc}") from exc

    def _day(self, text: str | None) -> date:
        if not text:
            return self.db.today()
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise StoreError(f"วันที่ไม่ถูกต้อง: {text!r} (ใช้ YYYY-MM-DD)") from exc

    def _optimizer(self, world: World) -> ORchestraOptimizer:
        h = world.hospital
        return ORchestraOptimizer(
            h.rooms, h.equipments, h.staff, h.beds, team_synergy=h.team_synergy,
            time_limit_s=self.time_limit_s, num_workers=self.workers,
        )

    @staticmethod
    def _problems(world: World, result: ScheduleResult) -> list[str]:
        h = world.hospital
        return validate_schedule(result, result.cases.values(), h.rooms, h.equipments, h.staff, h.beds)

    def _guard(self, world: World, after: ScheduleResult) -> None:
        """Reject a result that adds any conflict the stored schedule did not already have."""
        known = set(self._problems(world, world.result))
        new = [p for p in self._problems(world, after) if p not in known]
        if new:
            raise StoreError("ตารางที่คำนวณได้ไม่ผ่านการตรวจ: " + "; ".join(new[:3]))

    @staticmethod
    def _request(data: dict, request_id: uuid.UUID) -> dict:
        name = (data.get("patient_name") or "").strip()
        if not name:
            raise StoreError("กรุณากรอกชื่อผู้ป่วย")
        procedure_id = data.get("procedure_id")
        try:
            procedure_id = uuid.UUID(procedure_id) if procedure_id else None
        except ValueError as exc:
            raise StoreError("รหัสหัตถการไม่ถูกต้อง") from exc
        return {"request_id": request_id, "patient_name": name, "procedure_id": procedure_id,
                "raw_text": data.get("procedure")}

    def _record(self, conn, world: World, activity: Activity) -> dict:
        record = {"kind": activity.kind, "title": activity.title, "log": activity.log,
                  "changes": activity.changes, "notifications": activity.notifications, "actor": activity.actor}
        if not world.has_activity:
            self._fallback_activity.setdefault(world.day, []).append(activity)
        return record

    # --------------------------------------------------------------- reads

    def state(self, day: str | None = None) -> dict:
        with self._transaction(write=False) as conn:
            world = self.db.load(conn, self._day(day))
            activity = self.db.activity(conn, world) if world.has_activity else [
                a.__dict__ for a in reversed(self._fallback_activity.get(world.day, [])[-30:])
            ]
        return self._state(world, activity)

    def _state(self, world: World, activity: list[dict]) -> dict:
        schedule = world.result.to_dict()
        bookings = schedule["bookings"]
        for b in bookings:
            row = world.bookings.get(b["case_id"], {})
            for key, col in (("actual_start", "actual_start_time"), ("actual_end", "actual_end_time")):
                b[key] = fmt(world.minute(row[col])) if row.get(col) else None
        start = min([hm("08:00"), *[b["start_min"] for b in bookings]]) // 60 * 60
        now = world.now_min
        end = -(-max([hm("18:00"), *[b["cleanup_end_min"] for b in bookings],
                      *([now + 30] if 0 < now < 24 * 60 else [])]) // 60) * 60
        h = world.hospital
        usable: dict[str, int] = {}
        for u in h.equipments:
            if u.is_usable:
                usable[u.eq_type] = usable.get(u.eq_type, 0) + 1
        return {
            "mode": self.mode,
            "date": world.day.isoformat(),
            "planning_day": f"{world.day.isoformat()} · Supabase",
            "schedule": schedule,
            "axis": {"start": max(0, start), "end": min(24 * 60, end)},
            "options": {
                "rooms": [{"id": r.or_id, "type": r.room_type, "caps": sorted(r.capabilities)} for r in h.rooms],
                "surgeons": [
                    {"id": s.staff_id, "name": s.name, "shifts": [f"{fmt(a)}-{fmt(b)}" for a, b in s.shifts]}
                    for s in h.staff if s.role == "SURGEON" and s.is_active
                ],
                "equipment_types": [{"type": t, "units": n} for t, n in sorted(usable.items())],
                "capabilities": sorted({c for r in h.rooms for c in r.all_capabilities}),
                "bed_types": sorted({b.bed_type for b in h.beds}),
                "procedures": world.procedures,
                "cards": world.cards,
                "skills": staff_skills(h),
            },
            "activity": activity,
            **fatigue_view(h, world.result),
            "warnings": self._problems(world, world.result),
            "rls_off": world.rls_off,
            "activity_table": world.has_activity,
            "empty": not h.rooms or not h.staff,
        }

    def people(self) -> list[dict]:
        with self._transaction(write=False) as conn:
            return self.db.people(conn)

    def equipment(self, day: str | None = None) -> list[dict]:
        with self._transaction(write=False) as conn:
            return equipment_rows(self.db.load(conn, self._day(day)).hospital.equipments)

    def update_equipment(self, serial: str, status: str, ready_text: str | None, *,
                         day: str | None = None, actor: dict | None = None) -> dict:
        """CSSD / Equipment & Inventory input: unit status and when it is ready again."""
        ready = ready_minute(status, ready_text)
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self._day(day))
            if serial not in world.unit_ids:
                raise StoreError(f"ไม่พบเครื่องมือ {serial}")
            conn.execute(
                "UPDATE equipment_units SET current_status = %s, available_at = COALESCE(%s, NOW()) WHERE unit_id = %s",
                (status, world.ts(ready) if ready is not None else None, world.unit_ids[serial]),
            )
            after = self.db.load(conn, world.day)
            problems = [p for p in self._problems(after, after.result) if serial in p]
            when = f" พร้อม {fmt(ready)}" if ready is not None else ""
            record = self._record(conn, after, Activity("equipment", f"{serial}: {status}{when}", problems,
                                                        actor=actor_name(actor)))
            self.db.log_activity(conn, after, record)
            unit = next(u for u in after.hospital.equipments if u.unit_id == serial)
            return {"unit": equipment_rows([unit])[0], "warnings": problems}

    # ------------------------------------------------------------- intake

    def intake_context(self, day: str | None = None) -> IntakeContext:
        with self._transaction(write=False) as conn:
            world = self.db.load(conn, self._day(day))
            history = self.db.history(conn)
        surgeons = [{"id": s.staff_id, "name": s.name} for s in world.hospital.staff
                    if s.role == "SURGEON" and s.is_active]
        return IntakeContext(world.day, world.procedures, surgeons, world.cards, history)

    def record_actual(self, case_id: str, start_text: str | None, end_text: str | None, *,
                      day: str | None = None, actor: dict | None = None) -> dict:
        """Actual start / end of a case: the history the duration forecast learns from."""
        if not start_text and not end_text:
            raise StoreError("กรุณาใส่เวลาเริ่มหรือเวลาเสร็จ")
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self._day(day))
            a = world.result.assignments.get(case_id)
            if a is None:
                raise StoreError(f"ไม่พบเคส {case_id} ในวันที่ {world.day}")
            check_team(actor, a.staff_ids)
            row = world.bookings[case_id]
            start = world.ts(parse_time(start_text)) if start_text else row.get("actual_start_time")
            end = world.ts(parse_time(end_text)) if end_text else row.get("actual_end_time")
            if end is not None and start is None:
                raise StoreError("บันทึกเวลาเริ่มผ่าตัดก่อน")
            if end is not None and end <= start:
                end += timedelta(days=1)  # finished after midnight
            conn.execute(
                "UPDATE scheduled_surgeries SET actual_start_time = %s, actual_end_time = %s WHERE schedule_id = %s",
                (start, end, row["schedule_id"]),
            )
            planned = a.end_min - a.start_min
            minutes = round((end - start).total_seconds() / 60) if end is not None else None
            title = f"{case_id} เวลาจริง " + (f"{minutes} นาที (วางแผน {planned})" if minutes else
                                               f"เริ่ม {fmt(world.minute(start))}")
            record = self._record(conn, world, Activity("actual", title, [], actor=actor_name(actor)))
            self.db.log_activity(conn, world, record)
            return {"case_id": case_id, "actual_minutes": minutes, "planned_minutes": planned}

    def seed_history(self) -> dict:
        today = self.db.today()
        with self._transaction(write=True) as conn:
            if seed.has_history(conn):
                raise StoreError("ฐานข้อมูลมีเวลาผ่าตัดจริงอยู่แล้ว จึงไม่สร้างประวัติตัวอย่าง")
            world = self.db.load(conn, today)
            count = seed.seed_history(conn, world)
            record = self._record(conn, world, Activity(
                "seed", f"สร้างประวัติเวลาผ่าตัดตัวอย่าง {count} เคส (40 วันย้อนหลัง)", []))
            self.db.log_activity(conn, world, record)
        return {"history": count}

    # -------------------------------------------------------------- writes

    def slot_check(self, data: dict, start_text: str, *, commit: bool, day: str | None = None,
                   actor: dict | None = None) -> dict:
        start = parse_time(start_text)
        with self._transaction(write=commit) as conn:
            world = self.db.load(conn, self._day(day))
            if start < world.now_min:
                raise StoreError(f"เวลาที่ขอผ่านไปแล้ว (ตอนนี้ {fmt(world.now_min)} ของวันที่ {world.day})")
            request_id = uuid.uuid4()
            case = build_case(world.hospital, data,
                              case_code(request_id, data.get("urgency") or "ELECTIVE", set(world.result.cases)))
            request = self._request(data, request_id) if commit else None
            check = self._optimizer(world).check_slot(world.result, case, start, verbose=False)
            out = check.to_dict()
            out["booked"] = False
            if commit and check.assignment is not None:
                self._guard(world, check.result)
                activity = Activity(
                    "booking", f"จอง {case.case_id} {case.procedure}", check.log,
                    [{"case_id": c.case_id, "details": c.details} for c in check.result.changes],
                    check.result.to_dict()["notifications"], [case.case_id], actor_name(actor),
                )
                self.db.save(conn, world, check.result, {case.case_id: request},
                             activity=self._record(conn, world, activity))
                out["booked"] = True
            return out

    def emergency(self, data: dict, now_text: str, *, day: str | None = None, actor: dict | None = None) -> dict:
        now = parse_time(now_text)
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self._day(day))
            request_id = uuid.uuid4()
            case = build_case(world.hospital, {**data, "urgency": "EMERGENCY"},
                              case_code(request_id, "EMERGENCY", set(world.result.cases)), release=now)
            request = self._request(data, request_id)
            after = self._optimizer(world).insert_emergency(
                world.result, list(world.result.cases.values()), [case], now_min=now,
                scenario_name=f"Emergency {case.case_id}", verbose=False,
            )
            return self._save_change(conn, world, after, "emergency",
                                     f"ฉุกเฉิน {case.case_id} {case.procedure}", case.case_id,
                                     {case.case_id: request}, actor)

    def overrun(self, case_id: str, new_duration: int, now_text: str, *, day: str | None = None,
                actor: dict | None = None) -> dict:
        now = parse_time(now_text)
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self._day(day))
            current = world.result.assignments.get(case_id)
            if current is None:
                raise StoreError(f"ไม่พบเคส {case_id} ในตาราง")
            check_own_case(actor, current.staff_ids[0])
            if not current.start_min < now:
                raise StoreError(f"{case_id} ยังไม่เริ่ม (เริ่ม {fmt(current.start_min)}) ให้แก้เวลาผ่าตัดแทน")
            if current.start_min + new_duration <= now:
                raise StoreError("เวลาผ่าตัดใหม่ต้องจบหลังเวลาปัจจุบัน")
            after = self._optimizer(world).handle_overrun(
                world.result, list(world.result.cases.values()), case_id,
                new_duration_mins=new_duration, now_min=now, scenario_name=f"Overrun {case_id}", verbose=False,
            )
            return self._save_change(conn, world, after, "overrun",
                                     f"{case_id} ผ่าตัดเกินเวลา -> {new_duration} นาที", case_id, {}, actor)

    def _save_change(self, conn, world: World, after: ScheduleResult, kind: str, title: str,
                     case_id: str, new_requests: dict, actor: dict | None = None) -> dict:
        if not after.is_feasible:
            raise StoreError(f"คำนวณไม่สำเร็จ ({after.status})")
        self._guard(world, after)
        tired = self._optimizer(world).new_fatigue(world.result, after)
        data = after.to_dict()
        log = [f"[Master Orchestrator] {c['case_id']}: {'; '.join(c['details'])}" for c in data["changes"]]
        log += [f"[Master Orchestrator] NOT SCHEDULED {u['case_id']}: {'; '.join(u['reasons'])}"
                for u in data["unscheduled"]]
        log += fatigue_log(tired)
        activity = Activity(kind, title, log, data["changes"], data["notifications"],
                            [c["case_id"] for c in data["changes"]] or [case_id], actor_name(actor))
        self.db.save(conn, world, after, new_requests, activity=self._record(conn, world, activity))
        return {"changes": data["changes"], "notifications": data["notifications"],
                "unscheduled": data["unscheduled"], "log": log, "fatigue": [i.to_dict() for i in tired]}

    def seed_demo(self) -> dict:
        """Fill an empty database with the synthetic demo hospital and book today's demo cases."""
        today = self.db.today()
        with self._transaction(write=True) as conn:
            if not seed.is_empty(conn):
                raise StoreError("ฐานข้อมูลมีข้อมูลห้องหรือบุคลากรอยู่แล้ว จึงไม่ใส่ข้อมูลตัวอย่าง")
            ids = seed.seed_resources(conn, self.db.tz, today)
            world = self.db.load(conn, today)
            cases, requests = [], {}
            for number, c in enumerate(elective_day(), start=1):
                request_id = uuid.uuid4()
                code = case_code(request_id, c.urgency.value, {x.case_id for x in cases})
                cases.append(replace(c, case_id=code, surgeon_id=str(ids["staff"][c.surgeon_id]),
                                     patient_name=f"ผู้ป่วยตัวอย่าง {number}"))
                requests[code] = {"request_id": request_id, "patient_name": f"ผู้ป่วยตัวอย่าง {number}",
                                  "procedure_id": ids["procedures"][c.procedure], "raw_text": c.procedure}
            result = self._optimizer(world).solve_schedule(cases, "Demo day", verbose=False)
            history = seed.seed_history(conn, world)  # past cases with actual times for the forecast
            activity = Activity("seed", f"ใส่ข้อมูลตัวอย่างและจอง {len(result.assignments)} เคสของวันนี้",
                                [f"[Master Orchestrator] demo day: {len(result.assignments)} cases, status {result.status}"])
            self.db.save(conn, world, result, requests, activity=self._record(conn, world, activity))
        return {"booked": len(result.assignments), "history": history, "date": today.isoformat()}
