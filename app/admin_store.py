"""Hospital data pages (Supabase mode): rooms, planned room closures, staff,
shifts and leave, equipment types and units.

Every write runs in one transaction under the scheduler's advisory lock, is
recorded in ``orchestra_activity`` and reports the coming days whose bookings
no longer fit (e.g. a surgeon now on leave, a room now closed), so the
scheduler can re-plan those days.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import psycopg
from psycopg.types.json import Jsonb

from orchestra_engine import StaffRole

from .db_store import DatabaseStore
from .store import StoreError

CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,19}$")
TAG = re.compile(r"^[A-Z][A-Z0-9_]{1,49}$")
ROOM_STATUSES = ("AVAILABLE", "MAINTENANCE", "CLOSED")
ROOM_TYPES = ("STANDARD", "HYBRID", "NEGATIVE_PRESSURE")
CAPABILITIES = ("LAMINAR_FLOW", "LEAD_LINED_XRAY", "NEGATIVE_PRESSURE", "ROBOTIC_READY")
ROLES = (StaffRole.SURGEON, StaffRole.ANESTHESIOLOGIST, StaffRole.SCRUB_NURSE, StaffRole.CIRCULATING_NURSE)
SHIFT_TYPES = ("REGULAR", "ON_CALL", "LEAVE")
EQUIPMENT_CATEGORIES = ("HEAVY_MACHINE", "INSTRUMENT_TRAY", "OTHER")
MAX_DAYS = 120  # one shift / leave request
CHECK_DAYS = 31  # days ahead checked for bookings that no longer fit


def _tags(values, what: str) -> list[str]:
    out = sorted({str(v).strip().upper() for v in values or [] if str(v).strip()})
    bad = [v for v in out if not TAG.match(v)]
    if bad:
        raise StoreError(f"{what} ใช้ได้เฉพาะ A-Z 0-9 _ (เช่น ROBOTIC_READY): {', '.join(bad)}")
    return out


def _uuid(text: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(text))
    except ValueError as exc:
        raise StoreError(f"รหัส{what}ไม่ถูกต้อง") from exc


def _date(text: str, what: str = "วันที่") -> date:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"{what}ไม่ถูกต้อง: {text!r} (ใช้ YYYY-MM-DD)") from exc


def _time(text: str) -> time:
    try:
        hours, minutes = str(text).split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError) as exc:
        raise StoreError(f"เวลาไม่ถูกต้อง: {text!r} (ใช้ HH:MM)") from exc


def _number(value, low: float, high: float, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise StoreError(f"{what}ต้องเป็นตัวเลข") from exc
    if not low <= number <= high:
        raise StoreError(f"{what}ต้องอยู่ระหว่าง {low:g}-{high:g}")
    return number


class HospitalAdmin:
    def __init__(self, store: DatabaseStore) -> None:
        self.store = store
        self.db = store.db

    # ------------------------------------------------------------- helpers

    def _at(self, day: date, clock: time) -> datetime:
        return datetime.combine(day, clock, tzinfo=self.db.tz)

    def _local(self, ts: datetime) -> str:
        return ts.astimezone(self.db.tz).strftime("%Y-%m-%d %H:%M")

    def _columns(self, conn) -> set[tuple[str, str]]:
        return {
            (r["table_name"], r["column_name"])
            for r in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
            )
        }

    def _require(self, conn, table: str, column: str) -> None:
        if (table, column) not in self._columns(conn):
            raise StoreError("ฐานข้อมูลยังไม่ได้รัน migration "
                             "supabase/migrations/20261104000000_hospital_data_and_room_closures.sql")

    def _log(self, conn, title: str, log: list[str], actor: dict | None, day: date | None = None) -> None:
        cols = self._columns(conn)
        world = SimpleNamespace(day=day or self.db.today(), has_activity=("orchestra_activity", "activity_id") in cols,
                                has_actor_col=("orchestra_activity", "actor") in cols)
        actor_text = None if actor is None else f"{actor.get('name')} ({actor.get('role')})"
        self.db.log_activity(conn, world, {"kind": "admin", "title": title, "log": log, "changes": [],
                                           "notifications": [], "actor": actor_text})

    def _affected(self, conn, first: date, last: date) -> list[dict]:
        """Coming days (from today, at most CHECK_DAYS) whose bookings now break a rule."""
        today = self.db.today()
        first, last = max(first, today), min(last, today + timedelta(days=CHECK_DAYS))
        if first > last:
            return []
        days = [r["d"] for r in conn.execute(
            """SELECT DISTINCT (start_time AT TIME ZONE %s)::date AS d FROM scheduled_surgeries
               WHERE status IS DISTINCT FROM 'CANCELLED' AND start_time < %s AND end_time > %s ORDER BY d""",
            (str(self.db.tz), self._at(last + timedelta(days=1), time()), self._at(first, time())),
        )]
        out = []
        for day in days:
            world = self.db.load(conn, day)
            problems = self.store._problems(world, world.result)
            if problems:
                out.append({"date": day.isoformat(), "problems": problems[:5], "count": len(problems)})
        return out

    # -------------------------------------------------------------- rooms

    def rooms(self) -> dict:
        with self.store._transaction(write=False) as conn:
            cols = self._columns(conn)
            rooms = [
                {"code": r["room_code"], "type": r["room_type"], "capabilities": sorted(r["capabilities"] or []),
                 "base_turnover_mins": r["base_turnover_mins"], "infectious_turnover_mins": r["infectious_turnover_mins"],
                 "status": r["status"] or "AVAILABLE"}
                for r in conn.execute("SELECT * FROM operating_rooms ORDER BY room_code")
            ]
            closures = []
            if ("room_closures", "closure_id") in cols:
                closures = [
                    {"id": str(r["closure_id"]), "room": r["room_code"], "start": self._local(r["start_time"]),
                     "end": self._local(r["end_time"]), "reason": r["reason"], "by": r["created_by"]}
                    for r in conn.execute(
                        """SELECT c.*, o.room_code FROM room_closures c JOIN operating_rooms o USING (or_id)
                           WHERE c.end_time > NOW() - INTERVAL '1 day' ORDER BY c.start_time"""
                    )
                ]
        capabilities = sorted(set(CAPABILITIES) | {c for r in rooms for c in r["capabilities"]})
        return {"rooms": rooms, "closures": closures, "closures_ready": ("room_closures", "closure_id") in cols,
                "types": list(ROOM_TYPES), "capabilities": capabilities, "statuses": list(ROOM_STATUSES)}

    def save_room(self, data: dict, *, code: str | None = None, actor: dict | None = None) -> dict:
        new = code is None
        code = (data.get("code") if new else code) or ""
        if not CODE.match(code):
            raise StoreError("รหัสห้องใช้ได้ 1-20 ตัว (A-Z 0-9 - _ .) เช่น OR-5")
        room_type = (data.get("type") or "STANDARD").strip().upper()
        if not TAG.match(room_type):
            raise StoreError(f"ประเภทห้องไม่ถูกต้อง: {room_type}")
        caps = _tags(data.get("capabilities"), "ความสามารถของห้อง")
        status = data.get("status") or "AVAILABLE"
        if status not in ROOM_STATUSES:
            raise StoreError(f"สถานะห้องไม่ถูกต้อง: {status}")
        base = int(_number(data.get("base_turnover_mins", 30), 0, 240, "เวลาทำความสะอาดปกติ"))
        dirty = int(_number(data.get("infectious_turnover_mins", 60), 0, 480, "เวลาทำความสะอาดหลังเคสติดเชื้อ"))
        with self.store._transaction(write=True) as conn:
            values = (room_type, Jsonb(caps), base, dirty, status)
            if new:
                try:
                    conn.execute(
                        """INSERT INTO operating_rooms (room_type, capabilities, base_turnover_mins,
                               infectious_turnover_mins, status, room_code) VALUES (%s, %s, %s, %s, %s, %s)""",
                        (*values, code),
                    )
                except psycopg.errors.UniqueViolation as exc:
                    raise StoreError(f"มีห้อง {code} อยู่แล้ว") from exc
            else:
                cur = conn.execute(
                    """UPDATE operating_rooms SET room_type = %s, capabilities = %s, base_turnover_mins = %s,
                           infectious_turnover_mins = %s, status = %s WHERE room_code = %s""",
                    (*values, code),
                )
                if cur.rowcount != 1:
                    raise StoreError(f"ไม่พบห้อง {code}")
            detail = f"{code}: {room_type} [{', '.join(caps)}] สถานะ {status}, ทำความสะอาด {base}/{dirty} นาที"
            self._log(conn, ("เพิ่มห้อง " if new else "แก้ไขห้อง ") + code, [detail], actor)
            affected = [] if new else self._affected(conn, self.db.today(), self.db.today() + timedelta(days=CHECK_DAYS))
        return {"room": code, "affected": affected}

    def add_closure(self, data: dict, *, actor: dict | None = None) -> dict:
        start = self._at(_date(data.get("start_date"), "วันที่เริ่มปิด"), _time(data.get("start_time") or "00:00"))
        end = self._at(_date(data.get("end_date"), "วันที่เปิดใหม่"), _time(data.get("end_time") or "00:00"))
        if end <= start:
            raise StoreError("เวลาเปิดใหม่ต้องหลังเวลาเริ่มปิด")
        if end - start > timedelta(days=366):
            raise StoreError("ปิดห้องได้ครั้งละไม่เกิน 1 ปี (ถ้าปิดถาวร ให้เปลี่ยนสถานะห้องเป็น CLOSED)")
        reason = (data.get("reason") or "").strip()[:200] or None
        actor_text = None if actor is None else f"{actor.get('name')} ({actor.get('role')})"
        with self.store._transaction(write=True) as conn:
            self._require(conn, "room_closures", "closure_id")
            row = conn.execute("SELECT or_id FROM operating_rooms WHERE room_code = %s", (data.get("room"),)).fetchone()
            if row is None:
                raise StoreError(f"ไม่พบห้อง {data.get('room')}")
            closure_id = conn.execute(
                """INSERT INTO room_closures (or_id, start_time, end_time, reason, created_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING closure_id""",
                (row["or_id"], start, end, reason, actor_text),
            ).fetchone()["closure_id"]
            when = f"{self._local(start)} ถึง {self._local(end)}"
            self._log(conn, f"ปิดห้อง {data.get('room')} {when}", [reason or ""], actor, start.date())
            affected = self._affected(conn, start.date(), end.date())
        return {"id": str(closure_id), "affected": affected}

    def delete_closure(self, closure_id: str, *, actor: dict | None = None) -> dict:
        with self.store._transaction(write=True) as conn:
            self._require(conn, "room_closures", "closure_id")
            row = conn.execute(
                """DELETE FROM room_closures c USING operating_rooms o
                   WHERE c.closure_id = %s AND o.or_id = c.or_id RETURNING o.room_code, c.start_time, c.end_time""",
                (_uuid(closure_id, "การปิดห้อง"),),
            ).fetchone()
            if row is None:
                raise StoreError("ไม่พบรายการปิดห้องนี้")
            self._log(conn, f"ยกเลิกการปิดห้อง {row['room_code']} {self._local(row['start_time'])}", [], actor,
                      row["start_time"].astimezone(self.db.tz).date())
        return {"deleted": closure_id}

    # -------------------------------------------------------------- staff

    def staff(self) -> dict:
        with self.store._transaction(write=False) as conn:
            cols = self._columns(conn)
            people = [
                {"id": str(r["staff_id"]), "code": r["employee_code"], "name": r["full_name"], "role": r["role"],
                 "specialty": r["specialty"], "skills": sorted(r["skills"] or []),
                 "max_hours_per_week": float(r["max_hours_per_week"] or 48),
                 "max_hours_per_day": float(r.get("max_hours_per_day") or 10),
                 "min_rest_hours": float(r["min_rest_hours"]) if r.get("min_rest_hours") is not None else 8.0,
                 "active": bool(r["is_active"])}
                for r in conn.execute("SELECT * FROM staff ORDER BY is_active DESC, role, full_name")
            ]
        return {"staff": people, "roles": list(ROLES), "limits_ready": ("staff", "max_hours_per_day") in cols,
                "skills": sorted({s for p in people for s in p["skills"]})}

    def save_staff(self, data: dict, *, staff_id: str | None = None, actor: dict | None = None) -> dict:
        new = staff_id is None
        name = (data.get("name") or "").strip()
        code = (data.get("code") or "").strip()
        if not name or len(name) > 150:
            raise StoreError("กรุณาใส่ชื่อบุคลากร (ไม่เกิน 150 ตัวอักษร)")
        if not code or len(code) > 50:
            raise StoreError("กรุณาใส่รหัสพนักงาน (ไม่เกิน 50 ตัวอักษร)")
        role = data.get("role")
        if role not in ROLES:
            raise StoreError(f"บทบาทไม่ถูกต้อง: {role}")
        skills = _tags(data.get("skills"), "ทักษะ")
        weekly = _number(data.get("max_hours_per_week", 48), 1, 168, "ชั่วโมงสูงสุดต่อ 7 วัน")
        daily = _number(data.get("max_hours_per_day", 10), 1, 24, "ชั่วโมงสูงสุดต่อวัน")
        rest = _number(data.get("min_rest_hours", 8), 0, 24, "ชั่วโมงพักหลังเคสดึก")
        specialty = (data.get("specialty") or "").strip()[:100] or None
        active = bool(data.get("active", True))
        with self.store._transaction(write=True) as conn:
            cols = self._columns(conn)
            fields = {"employee_code": code, "full_name": name, "role": role, "specialty": specialty,
                      "skills": Jsonb(skills), "max_hours_per_week": round(weekly), "is_active": active}
            if ("staff", "max_hours_per_day") in cols:
                fields.update(max_hours_per_day=daily, min_rest_hours=rest)
            try:
                if new:
                    row = conn.execute(
                        f"INSERT INTO staff ({', '.join(fields)}) VALUES ({', '.join(['%s'] * len(fields))}) "
                        "RETURNING staff_id", list(fields.values()),
                    ).fetchone()
                    staff_id = str(row["staff_id"])
                else:
                    cur = conn.execute(
                        f"UPDATE staff SET {', '.join(f'{k} = %s' for k in fields)} WHERE staff_id = %s",
                        [*fields.values(), _uuid(staff_id, "บุคลากร")],
                    )
                    if cur.rowcount != 1:
                        raise StoreError("ไม่พบบุคลากรนี้")
            except psycopg.errors.UniqueViolation as exc:
                raise StoreError(f"รหัสพนักงาน {code} มีอยู่แล้ว") from exc
            detail = (f"{name} ({code}) {role} ทักษะ [{', '.join(skills)}] เกณฑ์ {weekly:g} ชม./7 วัน, "
                      f"{daily:g} ชม./วัน, พัก {rest:g} ชม." + ("" if active else " · ปิดใช้งาน"))
            self._log(conn, ("เพิ่มบุคลากร " if new else "แก้ไขบุคลากร ") + name, [detail], actor)
            affected = [] if new else self._affected(conn, self.db.today(), self.db.today() + timedelta(days=CHECK_DAYS))
        return {"id": staff_id, "affected": affected}

    # ------------------------------------------------------- shifts, leave

    def shifts(self, date_from: str | None, date_to: str | None, staff_id: str | None = None) -> dict:
        first = _date(date_from) if date_from else self.db.today()
        last = _date(date_to) if date_to else first + timedelta(days=6)
        if last < first or (last - first).days > 62:
            raise StoreError("ดูได้ครั้งละไม่เกิน 62 วัน")
        params = {"start": self._at(first, time()), "end": self._at(last + timedelta(days=1), time())}
        where = "s.shift_end > %(start)s AND s.shift_start < %(end)s"
        if staff_id:
            params["staff"] = _uuid(staff_id, "บุคลากร")
            where += " AND s.staff_id = %(staff)s"
        with self.store._transaction(write=False) as conn:
            cols = self._columns(conn)
            note = ", s.note" if ("staff_schedules", "note") in cols else ""
            rows = [
                {"id": str(r["schedule_id"]), "staff_id": str(r["staff_id"]), "name": r["full_name"],
                 "role": r["role"], "start": self._local(r["shift_start"]), "end": self._local(r["shift_end"]),
                 "type": r["shift_type"] or "REGULAR", "available": r["is_available"] is not False,
                 "note": r.get("note")}
                for r in conn.execute(
                    f"""SELECT s.schedule_id, s.staff_id, s.shift_start, s.shift_end, s.shift_type, s.is_available{note},
                               p.full_name, p.role
                        FROM staff_schedules s JOIN staff p USING (staff_id)
                        WHERE {where} ORDER BY s.shift_start, p.full_name""",
                    params,
                )
            ]
        return {"from": first.isoformat(), "to": last.isoformat(), "shifts": rows, "types": list(SHIFT_TYPES)}

    def add_shifts(self, data: dict, *, actor: dict | None = None) -> dict:
        kind = data.get("type") or "REGULAR"
        if kind not in SHIFT_TYPES:
            raise StoreError(f"ประเภทเวรไม่ถูกต้อง: {kind}")
        first, last = _date(data.get("date_from"), "วันที่เริ่ม"), _date(data.get("date_to"), "วันที่สิ้นสุด")
        if last < first:
            raise StoreError("วันที่สิ้นสุดต้องไม่ก่อนวันที่เริ่ม")
        if (last - first).days >= MAX_DAYS:
            raise StoreError(f"เพิ่มได้ครั้งละไม่เกิน {MAX_DAYS} วัน")
        weekdays = {int(d) for d in data.get("weekdays") or range(7) if 0 <= int(d) <= 6}
        start, end = _time(data.get("start") or "00:00"), _time(data.get("end") or "00:00")
        people = [_uuid(s, "บุคลากร") for s in data.get("staff_ids") or []]
        if not people:
            raise StoreError("เลือกบุคลากรอย่างน้อย 1 คน")
        note = (data.get("note") or "").strip()[:200] or None
        rows, day = [], first
        while day <= last:
            if day.weekday() in weekdays:
                t0 = self._at(day, start)
                t1 = self._at(day + timedelta(days=1) if end <= start else day, end)  # past midnight
                rows += [(sid, t0, t1, kind, note) for sid in people]
            day += timedelta(days=1)
        if not rows:
            raise StoreError("ไม่มีวันที่ตรงกับวันในสัปดาห์ที่เลือก")
        with self.store._transaction(write=True) as conn:
            cols = self._columns(conn)
            known = {r["staff_id"]: r["full_name"] for r in conn.execute(
                "SELECT staff_id, full_name FROM staff WHERE staff_id = ANY(%s)", (people,))}
            missing = [str(p) for p in people if p not in known]
            if missing:
                raise StoreError("ไม่พบบุคลากรบางคน")
            with conn.cursor() as cur:
                if ("staff_schedules", "note") in cols:
                    cur.executemany(
                        "INSERT INTO staff_schedules (staff_id, shift_start, shift_end, shift_type, note) "
                        "VALUES (%s, %s, %s, %s, %s)", rows)
                else:
                    cur.executemany(
                        "INSERT INTO staff_schedules (staff_id, shift_start, shift_end, shift_type) VALUES (%s, %s, %s, %s)",
                        [r[:4] for r in rows])
            label = {"REGULAR": "เวรปกติ", "ON_CALL": "เวร on-call", "LEAVE": "วันลา"}[kind]
            names = ", ".join(sorted(known.values()))
            self._log(conn, f"เพิ่ม{label} {first.isoformat()} ถึง {last.isoformat()}",
                      [f"{names}: {start.strftime('%H:%M')}-{end.strftime('%H:%M')} ({len(rows)} รายการ)"
                       + (f" · {note}" if note else "")], actor, first)
            affected = self._affected(conn, first, last + timedelta(days=1))
        return {"created": len(rows), "affected": affected}

    def delete_shifts(self, data: dict, *, actor: dict | None = None) -> dict:
        ids = [_uuid(i, "เวร") for i in data.get("ids") or []]
        if not ids:
            raise StoreError("เลือกเวรที่จะลบ")
        with self.store._transaction(write=True) as conn:
            rows = conn.execute(
                """DELETE FROM staff_schedules s USING staff p WHERE s.schedule_id = ANY(%s) AND p.staff_id = s.staff_id
                   RETURNING p.full_name, s.shift_start, s.shift_end, s.shift_type""",
                (ids,),
            ).fetchall()
            if not rows:
                raise StoreError("ไม่พบเวรที่จะลบ")
            first = min(r["shift_start"] for r in rows).astimezone(self.db.tz).date()
            last = max(r["shift_end"] for r in rows).astimezone(self.db.tz).date()
            self._log(conn, f"ลบเวร/วันลา {len(rows)} รายการ",
                      [f"{r['full_name']}: {r['shift_type']} {self._local(r['shift_start'])}-{self._local(r['shift_end'])}"
                       for r in rows[:20]], actor, first)
            affected = self._affected(conn, first, last)
        return {"deleted": len(rows), "affected": affected}

    # ---------------------------------------------------------- equipment

    def equipment(self) -> dict:
        with self.store._transaction(write=False) as conn:
            types = [
                {"id": str(r["eq_type_id"]), "name": r["name"], "category": r["category"],
                 "requires_sterilization": bool(r["requires_sterilization"]),
                 "sterilization_mins": r["sterilization_duration_mins"] or 0, "units": r["units"]}
                for r in conn.execute(
                    """SELECT t.*, (SELECT count(*) FROM equipment_units u WHERE u.eq_type_id = t.eq_type_id) AS units
                       FROM equipment_types t ORDER BY t.name"""
                )
            ]
            units = [
                {"serial": r["serial_number"], "type_id": str(r["eq_type_id"]), "type": r["name"],
                 "status": r["current_status"] or "AVAILABLE", "location": r["current_location"]}
                for r in conn.execute(
                    """SELECT u.*, t.name FROM equipment_units u JOIN equipment_types t USING (eq_type_id)
                       ORDER BY t.name, u.serial_number"""
                )
            ]
        return {"types": types, "units": units, "categories": list(EQUIPMENT_CATEGORIES)}

    def save_type(self, data: dict, *, type_id: str | None = None, actor: dict | None = None) -> dict:
        name = (data.get("name") or "").strip()
        if not name or len(name) > 150:
            raise StoreError("กรุณาใส่ชื่อชนิดเครื่องมือ (ไม่เกิน 150 ตัวอักษร)")
        category = data.get("category") or "INSTRUMENT_TRAY"
        if category not in EQUIPMENT_CATEGORIES:
            raise StoreError(f"หมวดเครื่องมือไม่ถูกต้อง: {category}")
        sterile = bool(data.get("requires_sterilization", True))
        minutes = int(_number(data.get("sterilization_mins", 120), 0, 1440, "เวลาอบฆ่าเชื้อ (นาที)"))
        with self.store._transaction(write=True) as conn:
            clash = conn.execute(
                "SELECT 1 FROM equipment_types WHERE lower(name) = lower(%s) AND eq_type_id IS DISTINCT FROM %s",
                (name, _uuid(type_id, "ชนิดเครื่องมือ") if type_id else None),
            ).fetchone()
            if clash:
                raise StoreError(f"มีชนิดเครื่องมือชื่อ {name} อยู่แล้ว")
            values = (name, category, sterile, minutes)
            if type_id is None:
                type_id = str(conn.execute(
                    """INSERT INTO equipment_types (name, category, requires_sterilization, sterilization_duration_mins)
                       VALUES (%s, %s, %s, %s) RETURNING eq_type_id""", values,
                ).fetchone()["eq_type_id"])
                title = f"เพิ่มชนิดเครื่องมือ {name}"
            else:
                cur = conn.execute(
                    """UPDATE equipment_types SET name = %s, category = %s, requires_sterilization = %s,
                           sterilization_duration_mins = %s WHERE eq_type_id = %s""",
                    (*values, _uuid(type_id, "ชนิดเครื่องมือ")),
                )
                if cur.rowcount != 1:
                    raise StoreError("ไม่พบชนิดเครื่องมือนี้")
                title = f"แก้ไขชนิดเครื่องมือ {name}"
            self._log(conn, title, [f"{category}, อบฆ่าเชื้อ {minutes if sterile else 0} นาที"], actor)
        return {"id": type_id}

    def save_unit(self, data: dict, *, serial: str | None = None, actor: dict | None = None) -> dict:
        new = serial is None
        serial = (data.get("serial") if new else serial) or ""
        if not serial.strip() or len(serial) > 100:
            raise StoreError("กรุณาใส่หมายเลขเครื่อง (ไม่เกิน 100 ตัวอักษร)")
        type_id = _uuid(data.get("type_id"), "ชนิดเครื่องมือ")
        location = (data.get("location") or "").strip()[:100] or None
        with self.store._transaction(write=True) as conn:
            if conn.execute("SELECT 1 FROM equipment_types WHERE eq_type_id = %s", (type_id,)).fetchone() is None:
                raise StoreError("ไม่พบชนิดเครื่องมือนี้")
            try:
                if new:
                    conn.execute(
                        """INSERT INTO equipment_units (eq_type_id, serial_number, current_status, current_location,
                               available_at) VALUES (%s, %s, 'AVAILABLE', %s, NOW() - INTERVAL '1 day')""",
                        (type_id, serial.strip(), location),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE equipment_units SET eq_type_id = %s, current_location = %s WHERE serial_number = %s",
                        (type_id, location, serial),
                    )
                    if cur.rowcount != 1:
                        raise StoreError(f"ไม่พบเครื่อง {serial}")
            except psycopg.errors.UniqueViolation as exc:
                raise StoreError(f"มีเครื่องหมายเลข {serial} อยู่แล้ว") from exc
            self._log(conn, ("เพิ่มเครื่องมือ " if new else "แก้ไขเครื่องมือ ") + serial, [location or ""], actor)
            affected = [] if new else self._affected(conn, self.db.today(), self.db.today() + timedelta(days=CHECK_DAYS))
        return {"serial": serial, "affected": affected}


def require_supabase(store) -> HospitalAdmin:
    if getattr(store, "mode", None) != "supabase":
        raise StoreError("หน้าข้อมูลโรงพยาบาลใช้ได้เมื่อเชื่อม Supabase เท่านั้น (โหมดข้อมูลจำลองแก้ข้อมูลไม่ได้)")
    return HospitalAdmin(store)
