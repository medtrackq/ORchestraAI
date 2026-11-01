"""Supabase mode: every request reads the planning day from the database.

Writes run in one transaction under an advisory lock (one scheduler write at
a time); the engine result is checked with ``validate_schedule`` and must not
add any conflict before it is saved. The exclusion constraints in the schema
are the final guard.
"""
from __future__ import annotations

import threading
import time as clock
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import psycopg


from orchestra_engine import ORchestraOptimizer, ScheduleResult, fmt, hm, thai
from orchestra_engine.demo import elective_day
from orchestra_engine.results import ScheduleChange

from . import blocks as bk
from . import cancellations as cx
from . import milestones as ms
from . import readiness as rd
from . import seed
from . import waitlist as wl
from .db import ConflictError, Database, DatabaseUnavailable, World, case_code
from .intake_service import IntakeContext
from .store import (Activity, StoreError, actor_name, build_case, check_own_case, check_team, cssd_event, equipment_rows,
                    facility_view, fatigue_log, fatigue_view, parse_time, ready_minute, room_options, staff_skills,
                    schedule_event, supply_options, with_roles)


NEED_WAITLIST = "ต้องรัน SQL ไฟล์ 015 (รายการรอผ่าตัด) ก่อน จึงจะกำหนดวันที่ควรผ่าตัดได้ (หน้า ⚙ สถานะระบบ → รัน SQL ที่ยังขาด)"


class DatabaseStore:
    mode = "supabase"

    def __init__(self, url: str, *, tz: str = "Asia/Bangkok", workers: int = 2, time_limit_s: float = 10.0) -> None:
        self.listener = None  # notify.Notifier.handle: told about every committed change
        self._outbox = threading.local()  # events of the open transaction, sent after it commits
        self.db = Database(url, tz)
        self.workers = workers
        self.time_limit_s = time_limit_s
        self._fallback_activity: dict[date, list[Activity]] = {}  # if the activity table is missing
        self._fallback_digests: set[str] = set()  # reminders sent, if app_settings is missing

    # ------------------------------------------------------------- helpers

    def _emit(self, event: dict) -> None:
        """Queue a notification event; it goes out only if the transaction commits."""
        if getattr(self._outbox, "depth", 0):
            self._outbox.events.append(event)
        elif self.listener is not None:
            self.listener(event)

    @contextmanager
    def _transaction(self, *, write: bool):
        shared = getattr(self._outbox, "shared", None)
        if shared is not None:  # inside batch(): join its transaction, in a savepoint of our own
            with shared.transaction():
                yield shared
            return
        outer = not getattr(self._outbox, "depth", 0)
        if outer:
            self._outbox.events = []
        self._outbox.depth = getattr(self._outbox, "depth", 0) + 1
        committed = False
        try:
            with self.db.connect() as conn:
                if write:
                    self.db.lock(conn)
                    self.db.settle_supplies(conn)  # cases that ended used what they held
                yield conn
            committed = True
        except DatabaseUnavailable as exc:
            raise StoreError(f"เชื่อมต่อฐานข้อมูลไม่ได้: {exc}") from exc
        except ConflictError as exc:
            raise StoreError(f"ตารางในฐานข้อมูลเปลี่ยนระหว่างคำนวณ กรุณาลองใหม่ ({exc})") from exc
        except psycopg.OperationalError as exc:
            raise StoreError(f"เชื่อมต่อฐานข้อมูลไม่ได้: {str(exc).splitlines()[0]}") from exc
        except psycopg.errors.UndefinedTable as exc:
            raise StoreError(f"ไม่พบตารางในฐานข้อมูล (รัน migration แล้วหรือยัง?): {exc}") from exc
        finally:
            self._outbox.depth -= 1
            if outer:
                events, self._outbox.events = self._outbox.events, []
                if committed and self.listener is not None:
                    for event in events:
                        self.listener(event)

    @contextmanager
    def batch(self):
        """One write transaction that every ``_transaction`` on this thread joins until it ends.

        Each joined call runs in its own savepoint, so one failing step is undone
        on its own while the rest carry on; raising out of the block undoes it all.
        """
        with self._transaction(write=True) as conn:
            self._outbox.shared = conn
            try:
                yield conn
            finally:
                self._outbox.shared = None

    def _day(self, text: str | None) -> date:
        if not text:
            return self.db.today()
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise StoreError(f"วันที่ไม่ถูกต้อง: {text!r} (ใช้ YYYY-MM-DD)") from exc

    def _optimizer(self, world: World, time_limit_s: float | None = None) -> ORchestraOptimizer:
        h = world.hospital
        return ORchestraOptimizer(
            h.rooms, h.equipments, h.staff, h.beds, team_synergy=h.team_synergy, supplies=h.supplies,
            time_limit_s=time_limit_s or self.time_limit_s, num_workers=self.workers,
        )

    @staticmethod
    def _problems(world: World, result: ScheduleResult) -> list[str]:
        h = world.hospital
        return h.validate(result)

    def _guard(self, world: World, after: ScheduleResult) -> None:
        """Reject a result that adds any conflict the stored schedule did not already have."""
        known = set(self._problems(world, world.result))
        new = [p for p in self._problems(world, after) if p not in known]
        if new:
            raise StoreError("ตารางที่คำนวณได้ไม่ผ่านการตรวจ: " + "; ".join(new[:3]))

    @staticmethod
    def _request_id(conn, data: dict, actor: dict | None = None) -> tuple[uuid.UUID, bool]:
        """A new request, or the waiting (PENDING) request being rebooked."""
        pending = data.get("pending_id")
        if not pending:
            return uuid.uuid4(), False
        try:
            request_id = uuid.UUID(pending)
        except ValueError as exc:
            raise StoreError("รหัสคำขอที่รอจัดไม่ถูกต้อง") from exc
        row = conn.execute("SELECT status, primary_surgeon_id FROM surgery_requests WHERE request_id = %s",
                           (request_id,)).fetchone()
        if row is None or row["status"] != "PENDING":
            raise StoreError("คำขอนี้ถูกจองหรือยกเลิกไปแล้ว กรุณาโหลดใหม่")
        check_own_case(actor, str(row["primary_surgeon_id"]))
        return request_id, True

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
                "raw_text": data.get("procedure"), "due_date": wl.parse_due(data.get("due_date"))}

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
            pending = self.db.pending(conn)
            readiness = self._readiness(conn, world)
            following = self._readiness_next(conn, world.day, world) if readiness is not None else None
            steps, steps_ready = self._milestones(conn, world)
            blocks = self.db.day_blocks(conn, world.day, self.db.today())
            waitlist_ready = self.db.has_waitlist(conn)
        return {**self._state(world, activity), "pending": pending, "waitlist_ready": waitlist_ready, "readiness": readiness or {},
                "readiness_ready": readiness is not None, "readiness_next": following, "cancel_reasons": cx.options(),
                "milestones": steps, "milestones_ready": steps_ready,
                "blocks": [b.to_dict() for b in blocks or []], "blocks_ready": blocks is not None}

    # ------------------------------------------------- steps on the day

    def _now(self, world: World) -> int | None:
        """Minutes since the planning day's midnight, on the day itself only."""
        return world.now_min if world.day == self.db.today() else None

    @staticmethod
    def _case_minutes(world: World, case_id: str, rows: dict | None) -> dict:
        row = world.bookings[case_id]
        minutes = {k: world.minute(ts) for k, ts in (rows or {}).get(str(row["request_id"]), {}).items()}
        if row.get("actual_start_time"):
            minutes["in_room"] = world.minute(row["actual_start_time"])
        if row.get("actual_end_time"):
            minutes["out_room"] = world.minute(row["actual_end_time"])
        return minutes

    def _milestones(self, conn, world: World) -> tuple[dict, bool]:
        rows = self.db.milestone_rows(conn, [r["request_id"] for r in world.bookings.values()])
        now = self._now(world)
        return {cid: ms.summarize(self._case_minutes(world, cid, rows), world.result.assignments[cid].start_min, now)
                for cid in world.bookings if cid in world.result.assignments}, rows is not None

    def milestone_update(self, case_id: str, data: dict, *, day: str | None = None, actor: dict | None = None) -> dict:
        """Record (or clear the latest) step of a case: holding, into the OR, incision, closure, out, left recovery."""
        step, time_text, clear = ms.parse_update(data)
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self._day(day))
            a = world.result.assignments.get(case_id)
            if a is None:
                raise StoreError(f"ไม่พบเคส {case_id} ในวันที่ {world.day}")
            check_own_case(actor, world.result.cases[case_id].surgeon_id)
            if step in ms.ACTUAL_STEPS:  # into / out of the OR are the actual times: the case's team only
                check_team(actor, a.staff_ids)
            row = world.bookings[case_id]
            rows = self.db.milestone_rows(conn, [row["request_id"]])
            if step in ms.IN_TABLE and rows is None:
                raise StoreError("บันทึกขั้นนี้ได้หลังรัน supabase/migrations/20261110000000_case_milestones.sql "
                                 "(เข้าห้อง / ออกห้อง บันทึกได้แล้ว)")
            minutes = self._case_minutes(world, case_id, rows)
            now = self._now(world)
            if clear:
                ms.check(minutes, step, None, clear=True)
                minute = None
            else:
                if time_text:
                    minute = ms.place(minutes, step, parse_time(time_text))
                elif now is not None:
                    minute = now
                else:
                    raise StoreError("วันอื่นที่ไม่ใช่วันนี้ ต้องใส่เวลา")
                if now is not None and minute > now + 5:
                    raise StoreError(f"บันทึกเวลาล่วงหน้าไม่ได้ (ตอนนี้ {fmt(now)})")
                ms.check(minutes, step, minute)
            at = world.ts(minute) if minute is not None else None
            if step in ("in_room", "out_room"):
                column = "actual_start_time" if step == "in_room" else "actual_end_time"
                conn.execute(f"UPDATE scheduled_surgeries SET {column} = %s WHERE schedule_id = %s",
                             (at, row["schedule_id"]))
            elif clear:
                conn.execute("DELETE FROM case_milestones WHERE request_id = %s AND step = %s", (row["request_id"], step))
            else:
                conn.execute(
                    """INSERT INTO case_milestones (request_id, step, at, recorded_by) VALUES (%s, %s, %s, %s)
                       ON CONFLICT (request_id, step) DO UPDATE SET at = EXCLUDED.at, recorded_by = EXCLUDED.recorded_by,
                           recorded_at = NOW()""", (row["request_id"], step, at, actor_name(actor)))
            minutes[step] = minute
            if step == "out_room" and minute is not None:
                used = minute - minutes["in_room"]
                record = self._record(conn, world, Activity(
                    "actual", f"{case_id} ออกจากห้องผ่าตัด: ใช้ห้อง {used} นาที (วางแผน {a.end_min - a.start_min})", [],
                    actor=actor_name(actor)))
                self.db.log_activity(conn, world, record)
            summary = ms.summarize(minutes, a.start_min, now)
        return {"case_id": case_id, "step": step, "milestones": summary}

    # ---------------------------------------------------- pre-op readiness

    def _readiness(self, conn, world: World) -> dict | None:
        """Checklist per case of the day (None before migration 009)."""
        ids = {cid: row["request_id"] for cid, row in world.bookings.items()}
        rows = self.db.readiness_rows(conn, ids.values())
        if rows is None:
            return None
        blood = {p["id"]: p.get("blood", False) for p in world.procedures}
        return {cid: rd.summarize(rows.get(str(rid), {}), blood.get(str(world.bookings[cid].get("procedure_id")), False))
                for cid, rid in ids.items()}

    def _readiness_next(self, conn, day: date, world: World | None = None) -> dict | None:
        """The next day with cases (within a few days) and its cases that are not ready yet."""
        first, cases = self.db.next_cases(conn, day, rd.LOOKAHEAD_DAYS)
        if first is None:
            return None
        rows = self.db.readiness_rows(conn, [c["request_id"] for c in cases]) or {}
        names = {s.staff_id: s.name for s in world.hospital.staff} if world else {}
        open_cases = []
        for c in cases:
            summary = rd.summarize(rows.get(c["request_id"], {}), c["blood"])
            if summary["status"] != "ready":
                open_cases.append({k: c[k] for k in ("case_id", "start", "end", "or_id", "procedure", "urgency",
                                                     "surgeon_id")}
                                  | {"surgeon": names.get(c["surgeon_id"]), "readiness": summary})
        return {"date": first.isoformat(), "total": len(cases), "cases": open_cases}

    def readiness_next(self) -> dict | None:
        """For the daily reminder: the next day with cases after today."""
        with self._transaction(write=False) as conn:
            if self.db.readiness_rows(conn, []) is None:
                return None
            return self._readiness_next(conn, self.db.today())

    def claim_digest(self, day: str, kind: str = "readiness") -> bool:
        """True once per day and kind of reminder across restarts and workers (kept in app_settings)."""
        key = f"{kind}_digest:{day}"
        with self._transaction(write=False) as conn:
            if not conn.execute("SELECT to_regclass('public.app_settings') IS NOT NULL AS ok").fetchone()["ok"]:
                if key in self._fallback_digests:
                    return False
                self._fallback_digests.add(key)
                return True
            row = conn.execute(
                """INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING RETURNING key""",
                (key, datetime.now(self.db.tz).isoformat(timespec="minutes")),
            ).fetchone()
        return row is not None

    def waitlist_due(self) -> list[dict]:
        """Waiting cases past or near the date they should be operated by (for the daily reminder)."""
        from .waitlist import alerts

        with self._transaction(write=False) as conn:
            if not self.db.has_waitlist(conn):
                return []
            return alerts(self.db.pending(conn))

    def readiness_update(self, case_id: str, data: dict, *, day: str | None = None, actor: dict | None = None) -> dict:
        """Tick, clear or flag one checklist item of a case (surgeons: their own cases)."""
        item, status, note = rd.parse_update(data)
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self._day(day))
            a = world.result.assignments.get(case_id)
            if a is None:
                raise StoreError(f"ไม่พบเคส {case_id} ในวันที่ {world.day}")
            case = world.result.cases[case_id]
            check_own_case(actor, case.surgeon_id)
            request_id = world.bookings[case_id]["request_id"]
            rows = self.db.readiness_rows(conn, [request_id])
            if rows is None:
                raise StoreError("ยังบันทึกความพร้อมไม่ได้: ให้รัน supabase/migrations/20261108000000_case_readiness.sql "
                                 "ใน Supabase ก่อน")
            before = rows.get(str(request_id), {}).get(item)
            if status == "PENDING":
                conn.execute("DELETE FROM case_readiness WHERE request_id = %s AND item = %s", (request_id, item))
            else:
                conn.execute(
                    """INSERT INTO case_readiness (request_id, item, status, note, updated_by) VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (request_id, item) DO UPDATE SET status = EXCLUDED.status, note = EXCLUDED.note,
                           updated_by = EXCLUDED.updated_by, updated_at = NOW()""",
                    (request_id, item, status, note, actor_name(actor)),
                )
            if status == "PROBLEM" and (before or {}).get("note") != note and world.day >= self.db.today():
                self._emit(rd.problem_event(world.day, case_id, item, f"{fmt(a.start_min)}-{fmt(a.end_min)} ห้อง {a.or_id}",
                                            case.surgeon_id, actor))
            summary = self._readiness(conn, world)[case_id]
        return {"case_id": case_id, "readiness": summary}

    def _state(self, world: World, activity: list[dict]) -> dict:
        schedule = with_roles(world.result.to_dict(), world.hospital)
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
                "rooms": room_options(h),
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
                "supplies": supply_options(h, world.result),
            },
            "activity": activity,
            **fatigue_view(h, world.result),
            **facility_view(h, world.result),
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
            if problems:
                self._emit(cssd_event(after.day, serial, status, ready, problems, actor))
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
            request_id, existing = self._request_id(conn, data, actor)
            case = self._new_case(conn, world, data, request_id)
            request = {**self._request(data, request_id), "existing": existing} if commit else None
            if request and request["due_date"] and not world.has("surgery_requests", "due_date"):
                raise StoreError(NEED_WAITLIST)
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
                self._emit(schedule_event("booking", world.day, check.result, case.case_id, actor))
            return out

    def _new_case(self, conn, world: World, data: dict, request_id: uuid.UUID):
        case = build_case(world.hospital, data,
                          case_code(request_id, data.get("urgency") or "ELECTIVE", set(world.result.cases)))
        blocks = self.db.day_blocks(conn, world.day, self.db.today())
        if blocks:  # a new elective case keeps out of other owners' OR blocks and prefers its own
            surgeon = next((s for s in world.hospital.staff if s.staff_id == case.surgeon_id), None)
            held, own = bk.case_limits(blocks, case.urgency, case.surgeon_id, surgeon.specialty if surgeon else None)
            case = replace(case, room_blocks=held, preferred_rooms=own)
        return case

    def find_slots(self, data: dict, start_text: str, *, day: str | None = None, days: int = wl.SEARCH_DAYS,
                   skip_weekends: bool = True, actor: dict | None = None) -> dict:
        """The first days (up to ``wl.WANT``) on which every agent accepts the case, from ``day`` on; books nothing.

        Each day is the same check as ``slot_check`` (the earliest time at or after the requested one).
        The search stops after ``wl.SEARCH_SECONDS`` and says where to carry on.
        """
        requested = parse_time(start_text)
        today = self.db.today()
        first = max(self._day(day), today)
        due = wl.parse_due(data.get("due_date"), today)
        if data.get("urgency") == "EMERGENCY":
            raise StoreError("เคสฉุกเฉินใช้แท็บฉุกเฉิน (แทรกในวันนี้ทันที)")
        plan = wl.search_days(first, days, skip_weekends)
        deadline = clock.monotonic() + wl.SEARCH_SECONDS
        found, skipped, searched, carry_on, checked = [], [], 0, None, None
        with self._transaction(write=False) as conn:
            request_id, _ = self._request_id(conn, data, actor)
            for d in plan:
                if len(found) >= wl.WANT:
                    break
                if searched and clock.monotonic() > deadline:  # at least one day per click
                    carry_on = d.isoformat()
                    break
                world = self.db.load(conn, d)
                searched, checked = searched + 1, d
                start = wl.search_start(requested, world.now_min if d == today else 0)
                if start is None:
                    skipped.append({"date": d.isoformat(), "label": wl.thai_date(d), "reason": "เลยเวลาของวันนี้แล้ว"})
                    continue
                case = self._new_case(conn, world, data, request_id)
                check = self._optimizer(world, wl.SEARCH_SOLVE_S).check_slot(world.result, case, start, verbose=False)
                row = wl.found_row(d, check, world.hospital, requested, due)
                if row:
                    found.append(row)
                else:
                    skipped.append(wl.not_found(d, check))
        checked = checked or (plan[-1] if plan else first)
        return {"from": (plan[0] if plan else first).isoformat(), "to": checked.isoformat(), "days": len(plan),
                "searched": searched, "found": found, "skipped": skipped, "carry_on": carry_on,
                "next_from": carry_on or (checked + timedelta(days=1)).isoformat(),
                "due_date": due.isoformat() if due else None,
                "requested": fmt(requested), "single_day": False}

    def add_waiting(self, data: dict, *, actor: dict | None = None) -> dict:
        """Put a request on the waiting list without a day (e.g. no free day yet, or the patient is not ready)."""
        urgency = data.get("urgency") or "ELECTIVE"
        if urgency == "EMERGENCY":
            raise StoreError("เคสฉุกเฉินต้องแทรกในตารางทันที ไม่เข้ารายการรอ")
        if data.get("pending_id"):
            raise StoreError("คำขอนี้อยู่ในรายการรออยู่แล้ว")
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self.db.today())
            request_id = uuid.uuid4()
            case = build_case(world.hospital, data, case_code(request_id, urgency, set()))
            check_own_case(actor, case.surgeon_id)
            request = self._request(data, request_id)
            if request["due_date"] and not world.has("surgery_requests", "due_date"):
                raise StoreError(NEED_WAITLIST)
            self.db.add_waiting(conn, world, case, request, request["due_date"])
            due = f" · ควรผ่าตัดภายใน {wl.thai_date(request['due_date'])}" if request["due_date"] else ""
            self.db.log_activity(conn, world, self._record(conn, world, Activity(
                "waitlist", f"เพิ่มเข้ารายการรอผ่าตัด {case.case_id} {case.procedure}{due}", [],
                actor=actor_name(actor))))
        return {"id": str(request_id), "code": case.case_id, "due_date": request["due_date"].isoformat() if request["due_date"] else None}

    def set_due(self, pending_id: str, due_text: str | None, *, actor: dict | None = None) -> dict:
        """Set or clear the date a waiting request should be operated by."""
        try:
            request_id = uuid.UUID(pending_id)
        except ValueError as exc:
            raise StoreError("รหัสคำขอไม่ถูกต้อง") from exc
        due = wl.parse_due(due_text, self.db.today())
        with self._transaction(write=True) as conn:
            if not self.db.has_waitlist(conn):
                raise StoreError(NEED_WAITLIST)
            row = conn.execute("SELECT status, primary_surgeon_id, urgency_level FROM surgery_requests WHERE request_id = %s",
                               (request_id,)).fetchone()
            if row is None or row["status"] != "PENDING":
                raise StoreError("คำขอนี้ถูกจองหรือยกเลิกไปแล้ว กรุณาโหลดใหม่")
            check_own_case(actor, str(row["primary_surgeon_id"]))
            conn.execute("UPDATE surgery_requests SET due_date = %s WHERE request_id = %s", (due, request_id))
            code = case_code(request_id, row["urgency_level"], set())
            world = self.db.load(conn, self.db.today())
            title = f"กำหนดวันที่ควรผ่าตัด {code}: {wl.thai_date(due)}" if due else f"ยกเลิกกำหนดวันที่ควรผ่าตัด {code}"
            self.db.log_activity(conn, world, self._record(conn, world, Activity("waitlist", title, [], actor=actor_name(actor))))
        return {"id": pending_id, "due_date": due.isoformat() if due else None}

    def emergency(self, data: dict, now_text: str, *, day: str | None = None, actor: dict | None = None) -> dict:
        now = parse_time(now_text)
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self._day(day))
            request_id, existing = self._request_id(conn, data, actor)
            case = build_case(world.hospital, {**data, "urgency": "EMERGENCY"},
                              case_code(request_id, "EMERGENCY", set(world.result.cases)), release=now)
            request = {**self._request(data, request_id), "existing": existing}
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
            raise StoreError(f"คำนวณไม่สำเร็จ ({thai.solver(after.status)})")
        self._guard(world, after)
        tired = self._optimizer(world).new_fatigue(world.result, after)
        data = after.to_dict()
        log = [f"[Master Orchestrator] {c['case_id']}: {'; '.join(c['details'])}" for c in data["changes"]]
        log += [f"[Master Orchestrator] จัดลงตารางไม่ได้ {u['case_id']}: {'; '.join(u['reasons'])}"
                for u in data["unscheduled"]]
        log += fatigue_log(tired)
        activity = Activity(kind, title, log, data["changes"], data["notifications"],
                            [c["case_id"] for c in data["changes"]] or [case_id], actor_name(actor))
        self.db.save(conn, world, after, new_requests, activity=self._record(conn, world, activity))
        self._emit(schedule_event(kind, world.day, after, case_id, actor, fatigue=len({i.staff_id for i in tired})))
        return {"changes": data["changes"], "notifications": data["notifications"],
                "unscheduled": data["unscheduled"], "log": log, "fatigue": [i.to_dict() for i in tired]}

    def replan(self, day: str | None = None, *, actor: dict | None = None) -> dict:
        """Re-plan the cases of a day that have not started (after leave, a room closure, ...),
        changing as little as possible."""
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self._day(day))
            if world.now_min >= 24 * 60:
                raise StoreError(f"วันที่ {world.day} ผ่านไปแล้ว")
            if not world.result.assignments:
                raise StoreError(f"วันที่ {world.day} ยังไม่มีเคส")
            problems = self._problems(world, world.result)
            after = self._optimizer(world).reschedule(
                world.result, list(world.result.cases.values()), now_min=world.now_min,
                scenario_name=f"Replan {world.day}", verbose=False,
            )
            title = f"จัดตารางวันที่ {world.day} ใหม่" + (f" (แก้ {len(problems)} จุดขัดกัน)" if problems else "")
            return self._save_change(conn, world, after, "replan", title, "", {}, actor)

    def cancel_booking(self, case_id: str, data: dict, *, day: str | None = None, actor: dict | None = None) -> dict:
        """Call off a booked case with a reason: it waits for a new day, or the request is given up."""
        reason, note, outcome = cx.parse(data)
        with self._transaction(write=True) as conn:
            world = self.db.load(conn, self._day(day))
            a = world.result.assignments.get(case_id)
            if a is None:
                raise StoreError(f"ไม่พบเคส {case_id} ในวันที่ {world.day}")
            case = world.result.cases[case_id]
            check_own_case(actor, case.surgeon_id)
            if a.frozen or world.bookings[case_id].get("actual_start_time"):
                raise StoreError(f"{case_id} เริ่มผ่าตัดแล้ว งดไม่ได้ (ถ้าหยุดกลางคัน ให้บันทึกเวลาเสร็จจริง)")
            after = replace(world.result, assignments={k: v for k, v in world.result.assignments.items() if k != case_id},
                            changes=[ScheduleChange(case_id, ["งดผ่าตัด: " + cx.LABELS[reason]], a, None)],
                            notifications=[], unscheduled={})
            what = "เลื่อน (รอจัดวันใหม่)" if outcome == "POSTPONED" else "ยกเลิกคำขอ"
            title = f"งดผ่าตัด {case_id}: {cx.LABELS[reason]} · {what}"
            record = self._record(conn, world, Activity("cancel", title, [note or ""], highlight=[case_id],
                                                       actor=actor_name(actor)))
            kept = self.db.has_cancellations(conn)
            self.db.save(conn, world, after, {}, activity=record,
                         cancel={"reason": reason, "note": note, "outcome": outcome, "by": actor_name(actor)})
            request_id = world.bookings[case_id]["request_id"]
            if outcome == "CANCELLED":
                self.db.cancel_request(conn, request_id)
            self._emit(schedule_event("cancel", world.day, after, case_id, actor))
        return {"case_id": case_id, "reason": reason, "outcome": outcome, "reason_kept": kept}

    def cancel_pending(self, pending_id: str, *, data: dict | None = None, actor: dict | None = None) -> dict:
        """Give up a request waiting to be rebooked (e.g. the patient went elsewhere)."""
        try:
            request_id = uuid.UUID(pending_id)
        except ValueError as exc:
            raise StoreError("รหัสคำขอไม่ถูกต้อง") from exc
        reason, note, _ = cx.parse(data, outcome_needed=False) if data and data.get("reason") else ("UNSPECIFIED", None, "")
        with self._transaction(write=True) as conn:
            owner = conn.execute("SELECT primary_surgeon_id FROM surgery_requests WHERE request_id = %s",
                                 (request_id,)).fetchone()
            if owner is not None:
                check_own_case(actor, str(owner["primary_surgeon_id"]))
            row = self.db.cancel_request(conn, request_id)
            if row is None:
                raise StoreError("คำขอนี้ถูกจองหรือยกเลิกไปแล้ว")
            world = self.db.load(conn, self.db.today())
            code = case_code(request_id, row["urgency_level"], set())
            self.db.record_given_up(conn, request_id, code, reason, note, actor_name(actor))
            self.db.log_activity(conn, world, self._record(conn, world, Activity(
                "cancel", f"ยกเลิกคำขอที่รอจัด {code}: {cx.LABELS[reason]}", [note or ""], actor=actor_name(actor))))
        return {"cancelled": pending_id, "reason": reason}

    # ------------------------------------------------------------- reports

    def _report_period(self, date_from: str | None, date_to: str | None) -> tuple[date, date]:
        last = self._day(date_to) if date_to else self.db.today()
        first = self._day(date_from) if date_from else last - timedelta(days=29)
        if first > last:
            raise StoreError("วันที่เริ่มต้องไม่หลังวันที่สิ้นสุด")
        if (last - first).days > 365:
            raise StoreError("ดูรายงานได้ครั้งละไม่เกิน 366 วัน")
        return first, last

    def _report_records(self, conn, first: date, last: date) -> tuple[list, list]:
        from .reports import CaseRecord, RoomInfo

        tz = self.db.tz
        start = datetime.combine(first, time(), tzinfo=tz)
        end = datetime.combine(last + timedelta(days=1), time(), tzinfo=tz)
        rows = list(conn.execute(
            """SELECT s.schedule_id, s.request_id, s.start_time, s.end_time, s.cleanup_end_time,
                      s.actual_start_time, s.actual_end_time, o.room_code, r.urgency_level, r.patient_hn,
                      COALESCE(p.name, r.raw_nlp_input, 'Surgery') AS procedure, st.full_name AS surgeon
               FROM scheduled_surgeries s
               JOIN surgery_requests r ON r.request_id = s.request_id
               JOIN operating_rooms o ON o.or_id = s.or_id
               LEFT JOIN procedures p ON p.procedure_id = r.procedure_id
               LEFT JOIN staff st ON st.staff_id = r.primary_surgeon_id
               WHERE s.status IS DISTINCT FROM 'CANCELLED' AND s.start_time >= %s AND s.start_time < %s""",
            (start, end),
        ))
        team: dict = {}
        if rows:
            for a in conn.execute(
                """SELECT a.schedule_id, st.full_name FROM resource_allocations a JOIN staff st ON st.staff_id = a.staff_id
                   WHERE a.schedule_id = ANY(%s)""", ([r["schedule_id"] for r in rows],),
            ):
                team.setdefault(a["schedule_id"], []).append(a["full_name"])
        cases = [
            CaseRecord(case_code(r["request_id"], r["urgency_level"], set()), r["room_code"], r["surgeon"] or "-",
                       r["procedure"], r["urgency_level"], r["start_time"], r["end_time"], r["cleanup_end_time"],
                       r["actual_start_time"], r["actual_end_time"], team.get(r["schedule_id"], []),
                       demo=(r["patient_hn"] or "").startswith("HIST-"))
            for r in rows
        ]
        cols = {(c["table_name"], c["column_name"]) for c in conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'")}
        closures: dict = {}
        if ("room_closures", "closure_id") in cols:
            for c in conn.execute(
                """SELECT o.room_code, c.start_time, c.end_time FROM room_closures c JOIN operating_rooms o USING (or_id)
                   WHERE c.end_time > %s AND c.start_time < %s""", (start, end),
            ):
                closures.setdefault(c["room_code"], []).append((c["start_time"], c["end_time"]))
        rooms = [
            RoomInfo(r["room_code"], r.get("elective_start"), r.get("elective_end"),
                     (r["status"] or "AVAILABLE") not in ("CLOSED", "OUT_OF_SERVICE"), closures.get(r["room_code"], []))
            for r in conn.execute("SELECT * FROM operating_rooms ORDER BY room_code")
        ]
        return cases, rooms

    def report(self, date_from: str | None = None, date_to: str | None = None) -> dict:
        """Management report for a period (default: the last 30 days) and the period before it."""
        from .reports import build_report, previous_period

        first, last = self._report_period(date_from, date_to)
        before = previous_period(first, last)
        with self._transaction(write=False) as conn:
            cases, rooms = self._report_records(conn, before[0], last)
            span = (first, last)
            counts, overrides = {}, 0
            if conn.execute("SELECT to_regclass('public.orchestra_activity') IS NOT NULL AS ok").fetchone()["ok"]:
                counts = {r["kind"]: r["n"] for r in conn.execute(
                    "SELECT kind, count(*) AS n FROM orchestra_activity WHERE planning_date BETWEEN %s AND %s "
                    "GROUP BY kind", span)}
                overrides = conn.execute(
                    """SELECT count(*) AS n FROM orchestra_activity a, jsonb_array_elements_text(a.agent_log) line
                       WHERE a.planning_date BETWEEN %s AND %s
                         AND (line LIKE %s OR line LIKE %s)""",
                    (*span, f"%{thai.FATIGUE_OVERRIDE}%", f"%{thai.FATIGUE_OVERRIDE_OLD}%"),
                ).fetchone()["n"]
            cancelled = conn.execute(
                "SELECT count(*) AS n FROM surgery_requests WHERE status = 'CANCELLED' "
                "AND preferred_date_start BETWEEN %s AND %s", span).fetchone()["n"]
            supplies = []
            if conn.execute("SELECT to_regclass('public.supply_reservations') IS NOT NULL AS ok").fetchone()["ok"]:
                supplies = [
                    {"code": r["code"], "name": r["name"], "unit": r["unit"], "used": r["used"]}
                    for r in conn.execute(
                        """SELECT i.code, i.name, i.unit, sum(r.quantity) AS used FROM supply_reservations r
                           JOIN supply_items i USING (item_id)
                           WHERE r.status = 'USED' AND (r.used_at AT TIME ZONE %s)::date BETWEEN %s AND %s
                           GROUP BY i.code, i.name, i.unit ORDER BY used DESC, i.name LIMIT 15""",
                        (str(self.db.tz), *span),
                    )
                ]
            waiting = len(self.db.pending(conn))
            cancellations = self._cancellations(conn, first, last)
            earlier_cancel = self._cancellations(conn, *before)
            stages, stages_ready = self._stage_cases(conn, first, last)
            block_use = self._block_usage(conn, first, last)
            from . import history_io

            history = history_io.compare(conn, cases, first, last, self.db.tz)
        extra = {
            "waiting": waiting, "cancelled": cancelled, "fatigue_overrides": overrides,
            "emergencies_inserted": counts.get("emergency", 0), "replans": counts.get("replan", 0),
            "overruns": counts.get("overrun", 0), "supplies_used": supplies, "mode": self.mode,
        }
        report = build_report(cases, rooms, first, last, self.db.tz, extra=extra)
        earlier = build_report(cases, rooms, *before, self.db.tz)
        report["cancellations"] = cancellations and cx.summarize(cancellations[0], report["cases"], waiting=cancellations[1])
        report["cancellations_ready"] = cancellations is not None
        report["stages"], report["stages_ready"] = ms.segment_stats(stages), stages_ready
        report["blocks"], report["blocks_ready"] = block_use or [], block_use is not None
        report["history"], report["history_ready"] = history or [], history is not None
        report["previous"] = {**earlier["kpi"], "cases": earlier["cases"]}
        if earlier_cancel is not None:
            report["previous"]["cancellations"] = cx.summarize(earlier_cancel[0], earlier["cases"], waiting=earlier_cancel[1])
        report["previous_range"] = [before[0].isoformat(), before[1].isoformat()]
        return report

    def _block_usage(self, conn, first: date, last: date) -> list[dict] | None:
        """Report: each block owner's hours, used by their own cases and by others (None before migration 012)."""
        if not self.db.has_blocks(conn):
            return None
        occ = bk.occurrences(self.db.block_rows(conn), first, last)
        given = {(str(r["block_id"]), r["day"]) for r in conn.execute(
            "SELECT block_id, day FROM or_block_releases WHERE day BETWEEN %s AND %s", (first, last))}
        for o in occ:
            o["given_back"] = (str(o["block_id"]), o["day"]) in given
        tz = self.db.tz
        cases = [
            {"room": r["room_code"], "start": r["s"], "end": r["e"],
             "surgeon_id": str(r["primary_surgeon_id"]) if r["primary_surgeon_id"] else None, "specialty": r["specialty"]}
            for r in conn.execute(
                """SELECT o.room_code, r.primary_surgeon_id, st.specialty,
                          COALESCE(s.actual_start_time, s.start_time) AS s,
                          CASE WHEN s.actual_end_time > s.actual_start_time THEN s.actual_end_time ELSE s.end_time END AS e
                   FROM scheduled_surgeries s JOIN surgery_requests r USING (request_id)
                   JOIN operating_rooms o ON o.or_id = s.or_id LEFT JOIN staff st ON st.staff_id = r.primary_surgeon_id
                   WHERE s.status IS DISTINCT FROM 'CANCELLED' AND s.start_time >= %s AND s.start_time < %s""",
                (datetime.combine(first, time(), tzinfo=tz), datetime.combine(last + timedelta(days=1), time(), tzinfo=tz)))
        ]
        return bk.usage(occ, cases, tz)

    def _stage_cases(self, conn, first: date, last: date) -> tuple[list[dict], bool]:
        """Each case's step times (minutes since the epoch) for the report's time-per-step table."""
        tz = self.db.tz
        rows = list(conn.execute(
            """SELECT request_id, actual_start_time, actual_end_time FROM scheduled_surgeries
               WHERE status IS DISTINCT FROM 'CANCELLED' AND start_time >= %s AND start_time < %s
                 AND actual_start_time IS NOT NULL""",
            (datetime.combine(first, time(), tzinfo=tz), datetime.combine(last + timedelta(days=1), time(), tzinfo=tz))))
        steps = self.db.milestone_rows(conn, [r["request_id"] for r in rows])
        epoch = lambda ts: ts.timestamp() / 60  # noqa: E731
        cases = []
        for r in rows:
            c = {k: epoch(v) for k, v in (steps or {}).get(str(r["request_id"]), {}).items()}
            c["in_room"] = epoch(r["actual_start_time"])
            if r["actual_end_time"]:
                c["out_room"] = epoch(r["actual_end_time"])
            cases.append(c)
        return cases, steps is not None

    def _cancellations(self, conn, first: date, last: date) -> tuple[list[dict], int] | None:
        """Cases called off that were planned in ``first``..``last``, and waiting requests given up then."""
        if not self.db.has_cancellations(conn):
            return None
        tz = str(self.db.tz)
        rows = [
            {"case_code": r["case_code"], "planned": r["planned_day"].isoformat(), "procedure": r["procedure_name"],
             "surgeon": r["full_name"], "reason": r["reason"], "outcome": r["outcome"], "note": r["note"],
             "same_day": r["cancelled_day"] == r["planned_day"]}
            for r in conn.execute(
                """SELECT c.*, s.full_name, (c.planned_start AT TIME ZONE %s)::date AS planned_day,
                          (c.cancelled_at AT TIME ZONE %s)::date AS cancelled_day
                   FROM case_cancellations c LEFT JOIN staff s ON s.staff_id = c.surgeon_id
                   WHERE c.planned_start IS NOT NULL AND (c.planned_start AT TIME ZONE %s)::date BETWEEN %s AND %s""",
                (tz, tz, tz, first, last))
        ]
        given_up = conn.execute(
            """SELECT count(*) AS n FROM case_cancellations WHERE planned_start IS NULL
               AND (cancelled_at AT TIME ZONE %s)::date BETWEEN %s AND %s""", (tz, first, last)).fetchone()["n"]
        return rows, given_up

    def report_cases(self, date_from: str | None = None, date_to: str | None = None) -> list:
        first, last = self._report_period(date_from, date_to)
        with self._transaction(write=False) as conn:
            return self._report_records(conn, first, last)[0]

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
                stocked = {k: v for k, v in c.required_supplies.items() if k in world.supply_ids}
                cases.append(replace(c, case_id=code, surgeon_id=str(ids["staff"][c.surgeon_id]),
                                     patient_name=f"ผู้ป่วยตัวอย่าง {number}", required_supplies=stocked))
                requests[code] = {"request_id": request_id, "patient_name": f"ผู้ป่วยตัวอย่าง {number}",
                                  "procedure_id": ids["procedures"][c.procedure], "raw_text": c.procedure}
            result = self._optimizer(world).solve_schedule(cases, "Demo day", verbose=False)
            history = seed.seed_history(conn, world)  # past cases with actual times for the forecast
            activity = Activity("seed", f"ใส่ข้อมูลตัวอย่างและจอง {len(result.assignments)} เคสของวันนี้",
                                [f"[Master Orchestrator] วันตัวอย่าง: จอง {len(result.assignments)} เคส "
                                 f"(ผล {thai.solver(result.status)})"])
            self.db.save(conn, world, result, requests, activity=self._record(conn, world, activity))
        return {"booked": len(result.assignments), "history": history, "date": today.isoformat()}
