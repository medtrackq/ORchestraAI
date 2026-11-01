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

from .db import supply_available
from .db_store import DatabaseStore
from .store import Forbidden, StoreError

CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,19}$")
TAG = re.compile(r"^[A-Z][A-Z0-9_]{1,49}$")
ROOM_STATUSES = ("AVAILABLE", "MAINTENANCE", "CLOSED")
ROOM_TYPES = ("STANDARD", "HYBRID", "NEGATIVE_PRESSURE")
CAPABILITIES = ("LAMINAR_FLOW", "LEAD_LINED_XRAY", "NEGATIVE_PRESSURE", "ROBOTIC_READY")
ROLES = (StaffRole.SURGEON, StaffRole.ANESTHESIOLOGIST, StaffRole.SCRUB_NURSE, StaffRole.CIRCULATING_NURSE)
SHIFT_TYPES = ("REGULAR", "ON_CALL", "LEAVE")
EQUIPMENT_CATEGORIES = ("HEAVY_MACHINE", "INSTRUMENT_TRAY", "OTHER")
SUPPLY_CATEGORIES = ("IMPLANT", "CONSUMABLE")
BED_TYPES = ("PACU", "ICU_SURGICAL")
BED_STATUSES = ("AVAILABLE", "CLOSED")
INFECTION_LEVELS = ("CLEAN", "CONTAMINATED", "DIRTY")
CARD_ROLES = {  # role -> most people a card may ask for (the primary surgeon is not counted)
    StaffRole.ANESTHESIOLOGIST: 3, StaffRole.SCRUB_NURSE: 4, StaffRole.CIRCULATING_NURSE: 4, StaffRole.SURGEON: 3,
}
HISTORY_MIN_CASES = 3  # suggest a new card time only from at least this many recorded cases
SUGGEST_GAP = 15  # ... when the recorded mean is this many minutes away from the card
SUPPLY_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,49}$")
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


def _hhmm(value) -> str | None:
    return None if value is None else value.strftime("%H:%M")


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
                 "status": r["status"] or "AVAILABLE",
                 "elective_start": _hhmm(r.get("elective_start")), "elective_end": _hhmm(r.get("elective_end"))}
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
                "types": list(ROOM_TYPES), "capabilities": capabilities, "statuses": list(ROOM_STATUSES),
                "hours_ready": ("operating_rooms", "elective_start") in cols}

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
        opens = _time(data["elective_start"]) if data.get("elective_start") else None
        closes = _time(data["elective_end"]) if data.get("elective_end") else None
        if opens and closes and closes <= opens:
            raise StoreError("เวลาปิดรับเคส elective ต้องหลังเวลาเปิด")
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
            if ("operating_rooms", "elective_start") in self._columns(conn):
                conn.execute("UPDATE operating_rooms SET elective_start = %s, elective_end = %s WHERE room_code = %s",
                             (opens, closes, code))
            hours = (f", elective {opens.strftime('%H:%M') if opens else '-'}-{closes.strftime('%H:%M') if closes else '-'}"
                     if opens or closes else "")
            detail = f"{code}: {room_type} [{', '.join(caps)}] สถานะ {status}, ทำความสะอาด {base}/{dirty} นาที{hours}"
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
            maintenance = []
            ready = ("equipment_maintenance", "maintenance_id") in self._columns(conn)
            if ready:
                maintenance = [
                    {"id": str(r["maintenance_id"]), "serial": r["serial_number"], "start": self._local(r["start_time"]),
                     "end": self._local(r["end_time"]), "reason": r["reason"], "by": r["created_by"]}
                    for r in conn.execute(
                        """SELECT m.*, u.serial_number FROM equipment_maintenance m JOIN equipment_units u USING (unit_id)
                           WHERE m.end_time > NOW() - INTERVAL '1 day' ORDER BY m.start_time"""
                    )
                ]
        return {"types": types, "units": units, "categories": list(EQUIPMENT_CATEGORIES),
                "maintenance": maintenance, "maintenance_ready": ready}

    def add_maintenance(self, data: dict, *, actor: dict | None = None) -> dict:
        """Planned maintenance of a unit (service visit, repair): not used then."""
        start = self._at(_date(data.get("start_date"), "วันที่เริ่ม"), _time(data.get("start_time") or "00:00"))
        end = self._at(_date(data.get("end_date"), "วันที่เสร็จ"), _time(data.get("end_time") or "00:00"))
        if end <= start:
            raise StoreError("เวลาเสร็จต้องหลังเวลาเริ่ม")
        if end - start > timedelta(days=366):
            raise StoreError("นัดซ่อมได้ครั้งละไม่เกิน 1 ปี (ถ้าเลิกใช้ ให้ตั้งสถานะเครื่องเป็นซ่อมบำรุง)")
        reason = (data.get("reason") or "").strip()[:200] or None
        actor_text = None if actor is None else f"{actor.get('name')} ({actor.get('role')})"
        with self.store._transaction(write=True) as conn:
            if ("equipment_maintenance", "maintenance_id") not in self._columns(conn):
                raise StoreError("ฐานข้อมูลยังไม่ได้รัน migration supabase/migrations/20261106000000_facility_and_cssd.sql")
            unit = conn.execute("SELECT unit_id FROM equipment_units WHERE serial_number = %s",
                                (data.get("serial"),)).fetchone()
            if unit is None:
                raise StoreError(f"ไม่พบเครื่อง {data.get('serial')}")
            mid = conn.execute(
                """INSERT INTO equipment_maintenance (unit_id, start_time, end_time, reason, created_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING maintenance_id""",
                (unit["unit_id"], start, end, reason, actor_text),
            ).fetchone()["maintenance_id"]
            self._log(conn, f"นัดซ่อมบำรุง {data.get('serial')} {self._local(start)} ถึง {self._local(end)}",
                      [reason or ""], actor, start.date())
            affected = self._affected(conn, start.date(), end.date())
        return {"id": str(mid), "affected": affected}

    def delete_maintenance(self, maintenance_id: str, *, actor: dict | None = None) -> dict:
        with self.store._transaction(write=True) as conn:
            row = conn.execute(
                """DELETE FROM equipment_maintenance m USING equipment_units u
                   WHERE m.maintenance_id = %s AND u.unit_id = m.unit_id RETURNING u.serial_number, m.start_time""",
                (_uuid(maintenance_id, "การซ่อมบำรุง"),),
            ).fetchone()
            if row is None:
                raise StoreError("ไม่พบรายการซ่อมบำรุงนี้")
            self._log(conn, f"ยกเลิกนัดซ่อม {row['serial_number']} {self._local(row['start_time'])}", [], actor)
        return {"deleted": maintenance_id}

    # ------------------------------------------------------- recovery beds

    def beds(self) -> dict:
        now = datetime.now(self.db.tz)
        with self.store._transaction(write=False) as conn:
            cols = self._columns(conn)
            reserved: dict = {}
            if ("scheduled_surgeries", "recovery_end_time") in cols:
                for r in conn.execute(
                    """SELECT s.reserved_bed_id, s.end_time, s.recovery_end_time, r.patient_hn FROM scheduled_surgeries s
                       JOIN surgery_requests r USING (request_id)
                       WHERE s.reserved_bed_id IS NOT NULL AND s.status IS DISTINCT FROM 'CANCELLED'
                         AND s.recovery_end_time > NOW() ORDER BY s.end_time"""
                ):
                    reserved.setdefault(r["reserved_bed_id"], []).append(
                        {"from": self._local(r["end_time"]), "until": self._local(r["recovery_end_time"])})
            beds = [
                {"code": r["bed_code"], "type": r["bed_type"], "status": r.get("status") or "AVAILABLE",
                 "note": r.get("note"),
                 "occupied_until": self._local(r["available_from"]) if r["available_from"] and r["available_from"] > now
                 else None,
                 "reserved": reserved.get(r["bed_id"], [])}
                for r in conn.execute("SELECT * FROM recovery_beds ORDER BY bed_type DESC, bed_code")
            ]
        return {"beds": beds, "types": list(BED_TYPES), "status_ready": ("recovery_beds", "status") in cols}

    def save_bed(self, data: dict, *, code: str | None = None, actor: dict | None = None) -> dict:
        new = code is None
        code = (data.get("code") if new else code) or ""
        if not CODE.match(code):
            raise StoreError("รหัสเตียงใช้ได้ 1-20 ตัว (A-Z 0-9 - _ .) เช่น ICU-3")
        bed_type = data.get("type") or "PACU"
        if bed_type not in BED_TYPES:
            raise StoreError(f"ประเภทเตียงไม่ถูกต้อง: {bed_type}")
        status = data.get("status") or "AVAILABLE"
        if status not in BED_STATUSES:
            raise StoreError(f"สถานะเตียงไม่ถูกต้อง: {status}")
        note = (data.get("note") or "").strip()[:200] or None
        with self.store._transaction(write=True) as conn:
            extra = ("recovery_beds", "status") in self._columns(conn)
            if new:
                try:
                    conn.execute("INSERT INTO recovery_beds (bed_code, bed_type, available_from) VALUES (%s, %s, NOW())",
                                 (code, bed_type))
                except psycopg.errors.UniqueViolation as exc:
                    raise StoreError(f"มีเตียง {code} อยู่แล้ว") from exc
            elif conn.execute("UPDATE recovery_beds SET bed_type = %s WHERE bed_code = %s",
                              (bed_type, code)).rowcount != 1:
                raise StoreError(f"ไม่พบเตียง {code}")
            if extra:
                conn.execute("UPDATE recovery_beds SET status = %s, note = %s WHERE bed_code = %s", (status, note, code))
            elif status != "AVAILABLE":
                raise StoreError("ปิดเตียงได้หลังรัน migration supabase/migrations/20261106000000_facility_and_cssd.sql")
            self._log(conn, ("เพิ่มเตียง " if new else "แก้ไขเตียง ") + code, [f"{bed_type} {status} {note or ''}"], actor)
            affected = [] if new else self._affected(conn, self.db.today(), self.db.today() + timedelta(days=CHECK_DAYS))
        return {"bed": code, "affected": affected}

    def occupy_bed(self, code: str, data: dict, *, actor: dict | None = None) -> dict:
        """A patient from outside the OR schedule (e.g. a ward patient in ICU) holds the bed until a time."""
        until = None
        if data.get("until_date"):
            until = self._at(_date(data["until_date"], "วันที่"), _time(data.get("until_time") or "00:00"))
            if until - datetime.now(self.db.tz) > timedelta(days=60):
                raise StoreError("ระบุได้ไม่เกิน 60 วันข้างหน้า")
        with self.store._transaction(write=True) as conn:
            cur = conn.execute(
                "UPDATE recovery_beds SET available_from = COALESCE(%s, NOW()), is_occupied = %s WHERE bed_code = %s",
                (until, until is not None, code),
            )
            if cur.rowcount != 1:
                raise StoreError(f"ไม่พบเตียง {code}")
            title = f"เตียง {code} มีผู้ป่วยถึง {self._local(until)}" if until else f"เตียง {code} ว่างแล้ว"
            self._log(conn, title, [(data.get("note") or "").strip()[:200]], actor)
            affected = self._affected(conn, self.db.today(), until.date() if until else self.db.today())
        return {"bed": code, "affected": affected}

    # ---------------------------------------------------- preference cards

    @staticmethod
    def _own(actor: dict | None, surgeon_id: str) -> None:
        """Surgeons may only see and change their own preference cards."""
        if actor and actor.get("role") == "surgeon" and actor.get("staff_id") != surgeon_id:
            raise Forbidden("ศัลยแพทย์แก้ได้เฉพาะ preference card ของตัวเอง")

    def cards(self, actor: dict | None = None) -> dict:
        from statistics import mean, pstdev

        surgeon_only = bool(actor and actor.get("role") == "surgeon")
        own = actor.get("staff_id") if surgeon_only else None  # None for a surgeon account not linked to staff
        with self.store._transaction(write=False) as conn:
            types = {r["eq_type_id"]: r["name"] for r in conn.execute("SELECT eq_type_id, name FROM equipment_types ORDER BY name")}
            surgeons = [{"id": str(r["staff_id"]), "name": r["full_name"], "active": r["is_active"]} for r in conn.execute(
                "SELECT staff_id, full_name, is_active FROM staff WHERE role = 'SURGEON' ORDER BY full_name")
                if not surgeon_only or str(r["staff_id"]) == own]
            procedures = [{"id": str(r["procedure_id"]), "name": r["name"], "duration": r["default_duration_mins"]}
                          for r in conn.execute("SELECT procedure_id, name, default_duration_mins FROM procedures ORDER BY name")]
            minutes: dict[tuple[str, str], list[float]] = {}
            for h in self.db.history(conn):
                minutes.setdefault((h.surgeon_id, h.procedure_id), []).append(h.minutes)
            rows = conn.execute(
                """SELECT c.*, s.full_name, p.name AS procedure_name FROM surgeon_preference_cards c
                   JOIN staff s ON s.staff_id = c.surgeon_id JOIN procedures p ON p.procedure_id = c.procedure_id
                   ORDER BY s.full_name, p.name"""
            ).fetchall()
        cards = []
        for r in rows:
            sid, pid = str(r["surgeon_id"]), str(r["procedure_id"])
            if surgeon_only and sid != own:
                continue
            equipment: dict[str, int] = {}
            for t in r["required_equipment_types"] or []:
                key = str(t)
                equipment[key] = equipment.get(key, 0) + 1
            seen = minutes.get((sid, pid), [])
            history = None
            if seen:
                avg = mean(seen)
                history = {"n": len(seen), "mean": round(avg), "sd": round(pstdev(seen)) if len(seen) > 1 else None}
                if len(seen) >= HISTORY_MIN_CASES and abs(avg - r["historical_avg_duration_mins"]) >= SUGGEST_GAP:
                    history["suggest"] = int(5 * round(avg / 5))
                    history["suggest_sd"] = max(5, int(5 * round(pstdev(seen) / 5)))
            cards.append({
                "id": str(r["card_id"]), "surgeon_id": sid, "surgeon": r["full_name"], "procedure_id": pid,
                "procedure": r["procedure_name"], "duration": r["historical_avg_duration_mins"],
                "stddev": r["historical_stddev_mins"], "roles": r["required_staff_roles"] or {},
                "equipment": [{"type_id": k, "name": types.get(uuid.UUID(k), "?"), "count": n} for k, n in equipment.items()],
                "notes": r["special_notes"], "history": history,
            })
        return {"cards": cards, "surgeons": surgeons, "procedures": procedures,
                "equipment_types": [{"id": str(k), "name": v} for k, v in types.items()],
                "roles": {role: limit for role, limit in CARD_ROLES.items()}, "own": own, "surgeon_only": surgeon_only}

    def save_card(self, data: dict, *, card_id: str | None = None, actor: dict | None = None) -> dict:
        duration = int(_number(data.get("duration"), 15, 1440, "เวลาผ่าตัด (นาที)"))
        stddev = int(_number(data.get("stddev", 15), 0, 240, "ความคลาดเคลื่อน (นาที)"))
        roles = {}
        for role, count in (data.get("roles") or {}).items():
            if role not in CARD_ROLES:
                raise StoreError(f"บทบาทไม่ถูกต้อง: {role}")
            roles[role] = int(_number(count, 0, CARD_ROLES[role], f"จำนวน {role}"))  # 0 = not needed (kept)
        equipment: list[uuid.UUID] = []
        for item in data.get("equipment") or []:
            count = int(_number(item.get("count", 1), 0, 5, "จำนวนเครื่องมือ"))
            equipment += [_uuid(item.get("type_id"), "ชนิดเครื่องมือ")] * count
        if len(equipment) > 20:
            raise StoreError("ใส่เครื่องมือได้ไม่เกิน 20 ชิ้นต่อ card")
        notes = (data.get("notes") or "").strip()[:1000] or None
        with self.store._transaction(write=True) as conn:
            if card_id is None:
                surgeon_id, procedure_id = _uuid(data.get("surgeon_id"), "ศัลยแพทย์"), _uuid(data.get("procedure_id"), "หัตถการ")
            else:
                row = conn.execute("SELECT surgeon_id, procedure_id FROM surgeon_preference_cards WHERE card_id = %s",
                                   (_uuid(card_id, "preference card"),)).fetchone()
                if row is None:
                    raise StoreError("ไม่พบ preference card นี้")
                surgeon_id, procedure_id = row["surgeon_id"], row["procedure_id"]
            self._own(actor, str(surgeon_id))
            surgeon = conn.execute("SELECT full_name FROM staff WHERE staff_id = %s AND role = 'SURGEON'",
                                   (surgeon_id,)).fetchone()
            procedure = conn.execute("SELECT name FROM procedures WHERE procedure_id = %s", (procedure_id,)).fetchone()
            if surgeon is None or procedure is None:
                raise StoreError("ไม่พบศัลยแพทย์หรือหัตถการนี้")
            known = {r["eq_type_id"] for r in conn.execute("SELECT eq_type_id FROM equipment_types WHERE eq_type_id = ANY(%s)",
                                                         (list(set(equipment)),))}
            if set(equipment) - known:
                raise StoreError("ไม่พบชนิดเครื่องมือบางรายการ")
            values = (duration, stddev, Jsonb(roles), Jsonb([str(e) for e in equipment]), notes)
            if card_id is None:
                try:
                    card_id = str(conn.execute(
                        """INSERT INTO surgeon_preference_cards (historical_avg_duration_mins, historical_stddev_mins,
                               required_staff_roles, required_equipment_types, special_notes, surgeon_id, procedure_id)
                           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING card_id""", (*values, surgeon_id, procedure_id),
                    ).fetchone()["card_id"])
                except psycopg.errors.UniqueViolation as exc:
                    raise StoreError(f"{surgeon['full_name']} มี preference card ของ {procedure['name']} อยู่แล้ว") from exc
                title = f"เพิ่ม preference card {surgeon['full_name']} · {procedure['name']}"
            else:
                conn.execute(
                    """UPDATE surgeon_preference_cards SET historical_avg_duration_mins = %s, historical_stddev_mins = %s,
                           required_staff_roles = %s, required_equipment_types = %s, special_notes = %s WHERE card_id = %s""",
                    (*values, _uuid(card_id, "preference card")))
                title = f"แก้ไข preference card {surgeon['full_name']} · {procedure['name']}"
            team = ", ".join(f"{role} x{n}" for role, n in roles.items() if n) or "ไม่มีทีมเพิ่ม"
            self._log(conn, title, [f"{duration} ± {stddev} นาที · {team} · เครื่องมือ {len(equipment)} ชิ้น"], actor)
        return {"id": card_id}

    def delete_card(self, card_id: str, *, actor: dict | None = None) -> dict:
        with self.store._transaction(write=True) as conn:
            row = conn.execute(
                """SELECT c.surgeon_id, s.full_name, p.name FROM surgeon_preference_cards c
                   JOIN staff s ON s.staff_id = c.surgeon_id JOIN procedures p ON p.procedure_id = c.procedure_id
                   WHERE c.card_id = %s""", (_uuid(card_id, "preference card"),)).fetchone()
            if row is None:
                raise StoreError("ไม่พบ preference card นี้")
            self._own(actor, str(row["surgeon_id"]))
            conn.execute("DELETE FROM surgeon_preference_cards WHERE card_id = %s", (_uuid(card_id, "preference card"),))
            self._log(conn, f"ลบ preference card {row['full_name']} · {row['name']}", [], actor)
        return {"deleted": card_id}

    # ---------------------------------------------------------- procedures

    def procedures(self) -> dict:
        with self.store._transaction(write=False) as conn:
            cols = self._columns(conn)
            rows = [
                {"id": str(r["procedure_id"]), "code": r["icd9_cm_code"], "name": r["name"],
                 "duration": r["default_duration_mins"], "caps": sorted(r["required_room_capabilities"] or []),
                 "icu": bool(r["requires_icu_post_op"]), "infection": r["infection_risk_level"] or "CLEAN",
                 "recovery_mins": r.get("recovery_mins"), "skills": r.get("required_staff_skills") or {}}
                for r in conn.execute("SELECT * FROM procedures ORDER BY name")
            ]
        return {"procedures": rows, "infection": list(INFECTION_LEVELS), "roles": list(ROLES),
                "capabilities": sorted(set(CAPABILITIES) | {c for p in rows for c in p["caps"]}),
                "recovery_ready": ("procedures", "recovery_mins") in cols,
                "skills_ready": ("procedures", "required_staff_skills") in cols}

    def save_procedure(self, data: dict, *, procedure_id: str | None = None, actor: dict | None = None) -> dict:
        name = (data.get("name") or "").strip()
        if not name or len(name) > 200:
            raise StoreError("กรุณาใส่ชื่อหัตถการ (ไม่เกิน 200 ตัวอักษร)")
        code = (data.get("code") or "").strip()[:20] or None
        duration = int(_number(data.get("duration", 60), 15, 1440, "เวลาผ่าตัดมาตรฐาน (นาที)"))
        caps = _tags(data.get("caps"), "ความสามารถของห้อง")
        infection = data.get("infection") or "CLEAN"
        if infection not in INFECTION_LEVELS:
            raise StoreError(f"ความเสี่ยงติดเชื้อไม่ถูกต้อง: {infection}")
        recovery = data.get("recovery_mins")
        recovery = None if recovery in (None, "") else int(_number(recovery, 0, 10080, "เวลาพักฟื้น (นาที)"))
        skills = {}
        for role, names in (data.get("skills") or {}).items():
            if role not in ROLES:
                raise StoreError(f"บทบาทไม่ถูกต้อง: {role}")
            if names:
                skills[role] = _tags(names, "ทักษะ")
        with self.store._transaction(write=True) as conn:
            cols = self._columns(conn)
            fields = {"name": name, "icd9_cm_code": code, "default_duration_mins": duration,
                      "required_room_capabilities": Jsonb(caps), "requires_icu_post_op": bool(data.get("icu")),
                      "infection_risk_level": infection}
            if ("procedures", "recovery_mins") in cols:
                fields["recovery_mins"] = recovery
            if ("procedures", "required_staff_skills") in cols:
                fields["required_staff_skills"] = Jsonb(skills)
            try:
                if procedure_id is None:
                    procedure_id = str(conn.execute(
                        f"INSERT INTO procedures ({', '.join(fields)}) VALUES ({', '.join(['%s'] * len(fields))}) "
                        "RETURNING procedure_id", list(fields.values())).fetchone()["procedure_id"])
                    title = f"เพิ่มหัตถการ {name}"
                else:
                    cur = conn.execute(f"UPDATE procedures SET {', '.join(f'{k} = %s' for k in fields)} WHERE procedure_id = %s",
                                       [*fields.values(), _uuid(procedure_id, "หัตถการ")])
                    if cur.rowcount != 1:
                        raise StoreError("ไม่พบหัตถการนี้")
                    title = f"แก้ไขหัตถการ {name}"
            except psycopg.errors.UniqueViolation as exc:
                raise StoreError(f"รหัส ICD-9-CM {code} ใช้กับหัตถการอื่นแล้ว") from exc
            self._log(conn, title, [f"{duration} นาที, ห้อง [{', '.join(caps)}], {infection}"
                                    + (", ICU" if data.get("icu") else "")
                                    + (f", พักฟื้น {recovery} นาที" if recovery is not None else "")], actor)
        return {"id": procedure_id}

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


class Inventory(HospitalAdmin):
    """Equipment & Inventory Agent data: single-use supplies and implants."""

    def _ready(self, conn) -> None:
        if ("supply_items", "item_id") not in self._columns(conn):
            raise StoreError("ฐานข้อมูลยังไม่ได้รัน migration supabase/migrations/20261105000000_supply_inventory.sql")

    def _item(self, conn, code: str) -> dict:
        row = conn.execute("SELECT * FROM supply_items WHERE code = %s", (code,)).fetchone()
        if row is None:
            raise StoreError(f"ไม่พบวัสดุ {code}")
        return row

    def inventory(self) -> dict:
        today = self.db.today()
        with self.store._transaction(write=True) as conn:  # write: settles cases that have ended
            if ("supply_items", "item_id") not in self._columns(conn):
                return {"ready": False, "items": [], "procedures": [], "categories": list(SUPPLY_CATEGORIES)}
            deliveries: dict = {}
            for r in conn.execute(
                """SELECT d.*, i.code FROM supply_deliveries d JOIN supply_items i USING (item_id)
                   WHERE d.status = 'EXPECTED' ORDER BY d.expected_date"""
            ):
                deliveries.setdefault(r["item_id"], []).append(r)
            held: dict = {}
            for r in conn.execute(
                """SELECT r.item_id, r.quantity, (s.start_time AT TIME ZONE %s)::date AS day
                   FROM supply_reservations r
                   JOIN scheduled_surgeries s ON s.request_id = r.request_id AND s.status IS DISTINCT FROM 'CANCELLED'
                   WHERE r.status = 'RESERVED'""", (str(self.db.tz),),
            ):
                held.setdefault(r["item_id"], []).append((r["day"], r["quantity"]))
            items = []
            for r in conn.execute("SELECT * FROM supply_items ORDER BY is_active DESC, category, name"):
                due = deliveries.get(r["item_id"], [])
                on_order = [(d["expected_date"], d["quantity"]) for d in due if d["expected_date"] >= today]
                free = supply_available(r["on_hand"], on_order, held.get(r["item_id"], []), today)
                reserved = held.get(r["item_id"], [])
                next_use = min((d for d, _ in reserved), default=None)
                items.append({
                    "code": r["code"], "name": r["name"], "category": r["category"], "unit": r["unit"],
                    "on_hand": r["on_hand"], "reorder_level": r["reorder_level"], "active": r["is_active"],
                    "reserved": sum(q for _, q in reserved), "reserved_cases": len(reserved),
                    "next_use": next_use.isoformat() if next_use else None,
                    "free": free, "low": r["is_active"] and free <= r["reorder_level"],
                    "free_later": r["on_hand"] + sum(q for _, q in on_order) - sum(q for _, q in reserved),
                    "deliveries": [{"id": str(d["delivery_id"]), "quantity": d["quantity"],
                                    "date": d["expected_date"].isoformat(), "late": d["expected_date"] < today,
                                    "note": d["note"]} for d in due],
                })
            needs: dict = {}
            for r in conn.execute(
                "SELECT ps.procedure_id, i.code, ps.quantity FROM procedure_supplies ps JOIN supply_items i USING (item_id)"
            ):
                needs.setdefault(str(r["procedure_id"]), {})[r["code"]] = r["quantity"]
            procedures = [{"id": str(r["procedure_id"]), "name": r["name"], "supplies": needs.get(str(r["procedure_id"]), {})}
                          for r in conn.execute("SELECT procedure_id, name FROM procedures ORDER BY name")]
        return {"ready": True, "items": items, "procedures": procedures, "categories": list(SUPPLY_CATEGORIES)}

    def save_item(self, data: dict, *, code: str | None = None, actor: dict | None = None) -> dict:
        new = code is None
        code = (data.get("code") if new else code) or ""
        if not SUPPLY_CODE.match(code):
            raise StoreError("รหัสวัสดุใช้ได้ 1-50 ตัว (A-Z 0-9 - _ .) เช่น KNEE-IMP-5")
        name = (data.get("name") or "").strip()
        if not name or len(name) > 200:
            raise StoreError("กรุณาใส่ชื่อวัสดุ (ไม่เกิน 200 ตัวอักษร)")
        category = data.get("category") or "CONSUMABLE"
        if category not in SUPPLY_CATEGORIES:
            raise StoreError(f"หมวดไม่ถูกต้อง: {category}")
        unit = (data.get("unit") or "ชิ้น").strip()[:30] or "ชิ้น"
        reorder = int(_number(data.get("reorder_level", 0), 0, 100000, "จุดสั่งซื้อ"))
        active = bool(data.get("active", True))
        with self.store._transaction(write=True) as conn:
            self._ready(conn)
            if new:
                stock = int(_number(data.get("on_hand", 0), 0, 100000, "จำนวนคงคลังเริ่มต้น"))
                try:
                    conn.execute(
                        """INSERT INTO supply_items (code, name, category, unit, on_hand, reorder_level, is_active)
                           VALUES (%s, %s, %s, %s, %s, %s, %s)""", (code, name, category, unit, stock, reorder, active))
                except psycopg.errors.UniqueViolation as exc:
                    raise StoreError(f"มีวัสดุรหัส {code} อยู่แล้ว") from exc
                self._log(conn, f"เพิ่มวัสดุ {code} {name}", [f"คงคลัง {stock} {unit}, จุดสั่งซื้อ {reorder}"], actor)
                return {"code": code, "affected": []}
            cur = conn.execute(
                """UPDATE supply_items SET name = %s, category = %s, unit = %s, reorder_level = %s, is_active = %s
                   WHERE code = %s""", (name, category, unit, reorder, active, code))
            if cur.rowcount != 1:
                raise StoreError(f"ไม่พบวัสดุ {code}")
            self._log(conn, f"แก้ไขวัสดุ {code} {name}", ["ใช้งาน" if active else "ปิดใช้งาน"], actor)
            affected = self._affected(conn, self.db.today(), self.db.today() + timedelta(days=CHECK_DAYS))
        return {"code": code, "affected": affected}

    def count_stock(self, code: str, data: dict, *, actor: dict | None = None) -> dict:
        """Stock take: set what is really on the shelf."""
        stock = int(_number(data.get("on_hand"), 0, 100000, "จำนวนที่นับได้"))
        reason = (data.get("note") or "").strip()[:200]
        with self.store._transaction(write=True) as conn:
            self._ready(conn)
            item = self._item(conn, code)
            conn.execute("UPDATE supply_items SET on_hand = %s WHERE code = %s", (stock, code))
            self._log(conn, f"นับสต็อก {code}: {item['on_hand']} -> {stock} {item['unit']}", [reason], actor)
            affected = (self._affected(conn, self.db.today(), self.db.today() + timedelta(days=CHECK_DAYS))
                        if stock < item["on_hand"] else [])
        return {"code": code, "on_hand": stock, "affected": affected}

    def receive(self, code: str, data: dict, *, actor: dict | None = None) -> dict:
        """Goods in: add to stock, closing the matching expected delivery if one is given."""
        delivery = data.get("delivery_id")
        with self.store._transaction(write=True) as conn:
            self._ready(conn)
            item = self._item(conn, code)
            if delivery:
                row = conn.execute(
                    """UPDATE supply_deliveries SET status = 'RECEIVED', received_at = NOW()
                       WHERE delivery_id = %s AND item_id = %s AND status = 'EXPECTED' RETURNING quantity""",
                    (_uuid(delivery, "การส่งของ"), item["item_id"]),
                ).fetchone()
                if row is None:
                    raise StoreError("ไม่พบรายการสั่งของนี้ (อาจรับไปแล้ว)")
                quantity = int(data.get("quantity") or row["quantity"])
            else:
                quantity = int(_number(data.get("quantity"), 1, 100000, "จำนวนที่รับ"))
            conn.execute("UPDATE supply_items SET on_hand = on_hand + %s WHERE code = %s", (quantity, code))
            self._log(conn, f"รับของ {code} +{quantity} {item['unit']}", [(data.get("note") or "").strip()[:200]], actor)
        return {"code": code, "received": quantity}

    def expect_delivery(self, code: str, data: dict, *, actor: dict | None = None) -> dict:
        quantity = int(_number(data.get("quantity"), 1, 100000, "จำนวนที่สั่ง"))
        when = _date(data.get("expected_date"), "วันที่คาดว่าของจะมา")
        if when < self.db.today():
            raise StoreError("วันที่คาดว่าของจะมาต้องไม่ใช่วันที่ผ่านไปแล้ว")
        actor_text = None if actor is None else f"{actor.get('name')} ({actor.get('role')})"
        with self.store._transaction(write=True) as conn:
            self._ready(conn)
            item = self._item(conn, code)
            delivery = conn.execute(
                """INSERT INTO supply_deliveries (item_id, quantity, expected_date, note, created_by)
                   VALUES (%s, %s, %s, %s, %s) RETURNING delivery_id""",
                (item["item_id"], quantity, when, (data.get("note") or "").strip()[:200] or None, actor_text),
            ).fetchone()["delivery_id"]
            self._log(conn, f"สั่งของ {code} +{quantity} {item['unit']} คาดว่ามา {when.isoformat()}", [], actor)
        return {"id": str(delivery)}

    def cancel_delivery(self, delivery_id: str, *, actor: dict | None = None) -> dict:
        with self.store._transaction(write=True) as conn:
            self._ready(conn)
            row = conn.execute(
                """UPDATE supply_deliveries d SET status = 'CANCELLED' FROM supply_items i
                   WHERE d.delivery_id = %s AND d.status = 'EXPECTED' AND i.item_id = d.item_id
                   RETURNING i.code, d.quantity, d.expected_date""",
                (_uuid(delivery_id, "การส่งของ"),),
            ).fetchone()
            if row is None:
                raise StoreError("ไม่พบรายการสั่งของนี้")
            self._log(conn, f"ยกเลิกการสั่ง {row['code']} +{row['quantity']} ({row['expected_date'].isoformat()})", [],
                      actor)
            affected = self._affected(conn, self.db.today(), self.db.today() + timedelta(days=CHECK_DAYS))
        return {"cancelled": delivery_id, "affected": affected}

    def set_procedure_supplies(self, procedure_id: str, data: dict, *, actor: dict | None = None) -> dict:
        needs = {str(k): int(_number(v, 0, 50, f"จำนวน {k}")) for k, v in (data.get("supplies") or {}).items()}
        with self.store._transaction(write=True) as conn:
            self._ready(conn)
            proc = conn.execute("SELECT name FROM procedures WHERE procedure_id = %s",
                                (_uuid(procedure_id, "หัตถการ"),)).fetchone()
            if proc is None:
                raise StoreError("ไม่พบหัตถการนี้")
            ids = {r["code"]: r["item_id"] for r in conn.execute(
                "SELECT code, item_id FROM supply_items WHERE code = ANY(%s)", (list(needs),))}
            unknown = [c for c in needs if c not in ids]
            if unknown:
                raise StoreError(f"ไม่พบวัสดุ: {', '.join(unknown)}")
            conn.execute("DELETE FROM procedure_supplies WHERE procedure_id = %s", (_uuid(procedure_id, "หัตถการ"),))
            rows = [(_uuid(procedure_id, "หัตถการ"), ids[c], q) for c, q in needs.items() if q > 0]
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO procedure_supplies (procedure_id, item_id, quantity) VALUES (%s, %s, %s)", rows)
            self._log(conn, f"วัสดุที่ใช้กับ {proc['name']}",
                      [", ".join(f"{c} x{q}" for c, q in needs.items() if q > 0) or "ไม่ใช้วัสดุ"], actor)
        return {"procedure_id": procedure_id, "supplies": {c: q for c, q in needs.items() if q > 0}}


def require_supabase(store) -> Inventory:
    if getattr(store, "mode", None) != "supabase":
        raise StoreError("หน้าข้อมูลโรงพยาบาลใช้ได้เมื่อเชื่อม Supabase เท่านั้น (โหมดข้อมูลจำลองแก้ข้อมูลไม่ได้)")
    return Inventory(store)
