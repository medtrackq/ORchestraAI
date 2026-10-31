"""Supabase / PostgreSQL data layer.

Reads one planning day from the schema in ``supabase/migrations`` into engine
objects (``World``) and writes engine results back. Times in the database are
TIMESTAMPTZ; the engine works in minutes from local midnight of the planning
day (``APP_TIMEZONE``, default Asia/Bangkok).

Engine ids are the human codes (room_code, serial_number, bed_code; staff
use their UUID and are shown by name); ``World`` keeps the maps back to the
database UUIDs.
"""
from __future__ import annotations

import logging
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from orchestra_engine import (
    Assignment,
    EquipmentUnit,
    InfectionRisk,
    OperatingRoom,
    RecoveryBed,
    ScheduleResult,
    StaffMember,
    SurgeryCase,
    Urgency,
)
from orchestra_engine.demo import Hospital

HORIZON = 24 * 60
WRITE_LOCK = 724_310_001  # pg_advisory_xact_lock key: one scheduler write at a time
TABLES = (
    "staff", "staff_schedules", "team_synergy", "equipment_types", "equipment_units", "operating_rooms",
    "recovery_beds", "procedures", "surgeon_preference_cards", "surgery_requests", "scheduled_surgeries",
    "resource_allocations",
)


log = logging.getLogger("orchestra.db")


class ConflictError(Exception):
    """The database changed while the schedule was being computed."""


class DatabaseUnavailable(Exception):
    """Could not open a connection; the message says what to fix in DATABASE_URL."""


# Query parameters libpq understands; others (e.g. Prisma's pgbouncer=true) are dropped.
_LIBPQ_PARAMS = {
    "sslmode", "sslrootcert", "sslcert", "sslkey", "channel_binding", "gssencmode", "connect_timeout",
    "application_name", "options", "target_session_attrs", "keepalives", "keepalives_idle", "user", "password",
    "dbname", "host", "port",
}


def normalize_url(raw: str) -> str:
    """Undo the usual paste accidents: `DATABASE_URL=` / `export` prefixes, quotes,
    spaces or new lines, a `jdbc:` prefix and ORM-only query parameters."""
    url = (raw or "").strip()
    if url.lower().startswith("export "):
        url = url[7:].strip()
    if url.upper().startswith("DATABASE_URL="):
        url = url.split("=", 1)[1].strip()
    if len(url) >= 2 and url[0] == url[-1] and url[0] in "\"'`":
        url = url[1:-1].strip()
    if url.startswith("jdbc:"):
        url = url[5:]
    if url.startswith(("postgres://", "postgresql://")):
        url = "".join(url.split())  # no spaces or line breaks belong in a URI (key=value form keeps them)
        query_at = url.find("?", url.rfind("@") + 1)
        if query_at != -1:
            kept = [p for p in url[query_at + 1:].split("&") if p.split("=", 1)[0] in _LIBPQ_PARAMS]
            url = url[:query_at] + ("?" + "&".join(kept) if kept else "")
    return url


def _bad_percent(text: str) -> bool:
    """True if a % is not followed by two hex digits (a raw % in a password)."""
    i = text.find("%")
    while i != -1:
        code = text[i + 1:i + 3]
        if len(code) < 2 or any(c not in "0123456789abcdefABCDEF" for c in code):
            return True
        i = text.find("%", i + 3)
    return False


def check_url(url: str) -> str | None:
    """Catch the usual copy-paste mistakes before the driver reports something misleading."""
    if "[YOUR-PASSWORD]" in url or "[YOUR_PASSWORD]" in url:
        return "ยังไม่ได้แทน [YOUR-PASSWORD] ใน DATABASE_URL ด้วยรหัสผ่านฐานข้อมูลจริง"
    if not url.startswith(("postgres://", "postgresql://")):
        try:  # libpq key=value form ("host=... dbname=...") is valid too
            if psycopg.conninfo.conninfo_to_dict(url).get("host"):
                return None
        except psycopg.ProgrammingError:
            pass
        return ("DATABASE_URL ต้องเป็นข้อความแบบ URI ที่ขึ้นต้นด้วย postgresql:// "
                "(ใน Supabase → Connect เลือก Session pooler แบบ URI ไม่ใช่ PSQL, JDBC หรือ .NET)")
    rest = url.split("://", 1)[1]
    if "@" not in rest:
        return "DATABASE_URL ไม่มีชื่อผู้ใช้และรหัสผ่าน (ต้องมีรูปแบบ postgresql://ชื่อผู้ใช้:รหัสผ่าน@host:5432/postgres)"
    userinfo = rest[:rest.rfind("@")]
    if any(ch in userinfo for ch in "/?#[] @") or _bad_percent(userinfo):
        return ("รหัสผ่านใน DATABASE_URL มีอักขระพิเศษ (เช่น @ # / ? % หรือเว้นวรรค) "
                "ให้ตั้งรหัสฐานข้อมูลใหม่ที่มีแต่ตัวอักษรภาษาอังกฤษกับตัวเลข")
    return None


def explain_connection_error(exc: Exception) -> str:
    """Turn a driver error into a fix the owner can make (never echoes secrets)."""
    low = str(exc).lower()
    if "password authentication failed" in low:
        return "รหัสผ่านใน DATABASE_URL ไม่ถูกต้อง (แทน [YOUR-PASSWORD] ด้วยรหัสฐานข้อมูลจริงแล้วหรือยัง?)"
    if "tenant or user not found" in low:
        return ("ชื่อผู้ใช้ใน DATABASE_URL ไม่ตรงกับโปรเจกต์ Supabase "
                "(แบบ Session pooler ต้องเป็น postgres.<project-ref>) หรือโปรเจกต์ถูก pause อยู่")
    if any(s in low for s in ("network is unreachable", "cannot assign requested address",
                              "could not translate host", "name or service not known", "nodename nor servname",
                              "no address associated")):
        return ("ต่อเซิร์ฟเวอร์ฐานข้อมูลไม่ได้ ให้ใช้ connection string แบบ Session pooler "
                "(host ลงท้าย pooler.supabase.com) ไม่ใช่ Direct connection")
    if "timeout" in low or "timed out" in low:
        return "เชื่อมต่อฐานข้อมูลไม่ทันเวลา ลองใหม่ หรือเช็กว่าโปรเจกต์ Supabase ไม่ได้ถูก pause"
    if any(s in low for s in ("invalid connection option", "missing \"=\"")):
        return "DATABASE_URL ต้องเป็นข้อความแบบ URI ที่ขึ้นต้นด้วย postgresql:// อย่างเดียว"
    if any(s in low for s in ("invalid dsn", "invalid uri", "invalid port", "invalid integer value",
                              "percent-encoded")):
        return "รูปแบบ DATABASE_URL ไม่ถูกต้อง (รหัสผ่านควรมีแต่ตัวอักษรภาษาอังกฤษกับตัวเลข)"
    if "connection refused" in low:
        return "เซิร์ฟเวอร์ฐานข้อมูลปฏิเสธการเชื่อมต่อ (เช็ก host และ port ใน DATABASE_URL)"
    return "เชื่อมต่อฐานข้อมูลไม่ได้ (ดูรายละเอียดใน Render → Logs)"


@dataclass
class World:
    """Everything the scheduler needs for one planning day."""

    day: date
    start: datetime  # local midnight as an aware datetime
    now_min: int
    hospital: Hospital
    room_ids: dict[str, uuid.UUID]
    unit_ids: dict[str, uuid.UUID]
    bed_ids: dict[str, uuid.UUID]
    type_ids: dict[str, uuid.UUID]  # equipment type key (name) -> eq_type_id
    procedures: list[dict]
    cards: list[dict]
    result: ScheduleResult
    bookings: dict[str, dict] = field(default_factory=dict)  # case_id -> scheduled_surgeries row
    has_recovery_col: bool = False
    has_activity: bool = False
    rls_off: list[str] = field(default_factory=list)
    has_actor_col: bool = False

    def minute(self, ts: datetime | None, default: int = 0) -> int:
        if ts is None:
            return default
        return int((ts - self.start).total_seconds() // 60)

    def ts(self, minute: int) -> datetime:
        return self.start + timedelta(minutes=minute)


def _clip(a: int, b: int) -> tuple[int, int] | None:
    a, b = max(a, 0), min(b, HORIZON)
    return (a, b) if b > a else None


def _subtract(windows: list[tuple[int, int]], holes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    for h0, h1 in holes:
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


def _merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for a, b in sorted(windows):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def case_code(request_id: uuid.UUID, urgency: str, taken: set[str]) -> str:
    letter = {"EMERGENCY": "E", "URGENT": "U"}.get(urgency, "R")
    for size in (6, 8, 12, 32):
        code = f"{letter}-{request_id.hex[:size].upper()}"
        if code not in taken:
            return code
    raise RuntimeError("duplicate request id")


class Database:
    def __init__(self, url: str, tz: str = "Asia/Bangkok") -> None:
        self.url = normalize_url(url)
        self.tz = ZoneInfo(tz)

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection]:
        """One transaction per block; committed on success, rolled back on error."""
        kwargs = {"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": 15}
        local = any(h in self.url for h in ("localhost", "127.0.0.1", "/var/run", "host=/"))
        if "sslmode=" not in self.url and not local:
            kwargs["sslmode"] = "require"
        problem = check_url(self.url)
        if problem:
            log.error("DATABASE_URL rejected before connecting: %s", problem)
            raise DatabaseUnavailable(problem)
        try:
            conn = psycopg.connect(self.url, **kwargs)
        except psycopg.Error as exc:
            log.error("database connection failed: %s", exc)  # libpq never includes the password
            raise DatabaseUnavailable(explain_connection_error(exc)) from exc
        with conn:
            yield conn

    def today(self) -> date:
        return datetime.now(self.tz).date()

    @staticmethod
    def lock(conn: psycopg.Connection) -> None:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (WRITE_LOCK,))

    # ------------------------------------------------------------------ read

    def load(self, conn: psycopg.Connection, day: date) -> World:
        start = datetime(day.year, day.month, day.day, tzinfo=self.tz)
        end = start + timedelta(minutes=HORIZON)
        now = datetime.now(self.tz)
        now_min = 0 if day > now.date() else HORIZON if day < now.date() else int((now - start).total_seconds() // 60)
        span = {"start": start, "end": end}

        def minute(ts: datetime | None, default: int = 0) -> int:
            return default if ts is None else int((ts - start).total_seconds() // 60)

        columns = {
            (r["table_name"], r["column_name"])
            for r in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'"
            )
        }
        has_recovery_col = ("scheduled_surgeries", "recovery_end_time") in columns
        has_activity = ("orchestra_activity", "activity_id") in columns
        has_actor_col = ("orchestra_activity", "actor") in columns
        rls = {
            r["relname"]: r["relrowsecurity"]
            for r in conn.execute(
                "SELECT relname, relrowsecurity FROM pg_class "
                "WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' AND relname = ANY(%s)",
                (list(TABLES),),
            )
        }

        rooms, room_ids, room_codes = [], {}, {}
        for r in conn.execute("SELECT * FROM operating_rooms ORDER BY room_code"):
            rooms.append(OperatingRoom(
                r["room_code"], r["room_type"], r["capabilities"] or [],
                r["base_turnover_mins"] or 0, r["infectious_turnover_mins"] or 0,
                status=r["status"] or "AVAILABLE",
            ))
            room_ids[r["room_code"]] = r["or_id"]
            room_codes[r["or_id"]] = r["room_code"]

        shifts: dict[uuid.UUID, list] = {}
        leave: dict[uuid.UUID, list] = {}
        for r in conn.execute(
            "SELECT staff_id, shift_start, shift_end, shift_type, is_available FROM staff_schedules "
            "WHERE shift_end > %(start)s AND shift_start < %(end)s", span,
        ):
            window = _clip(minute(r["shift_start"]), minute(r["shift_end"]))
            if window is None:
                continue
            off = (r["shift_type"] or "REGULAR") == "LEAVE" or r["is_available"] is False
            (leave if off else shifts).setdefault(r["staff_id"], []).append(window)
        staff, names = [], {}
        for r in conn.execute("SELECT * FROM staff ORDER BY full_name"):
            windows = _subtract(_merge(shifts.get(r["staff_id"], [])), leave.get(r["staff_id"], []))
            sid = str(r["staff_id"])
            staff.append(StaffMember(
                sid, r["full_name"], r["role"], windows, skills=r["skills"] or [],
                specialty=r["specialty"], max_weekly_hours=float(r["max_hours_per_week"] or 48),
                current_weekly_hours=float(r["current_weekly_hours"] or 0), is_active=bool(r["is_active"]),
            ))
            names[sid] = r["full_name"]

        type_rows = list(conn.execute("SELECT * FROM equipment_types ORDER BY name"))
        name_count = Counter(t["name"] for t in type_rows)
        type_keys = {
            t["eq_type_id"]: t["name"] if name_count[t["name"]] == 1 else f"{t['name']} #{str(t['eq_type_id'])[:4]}"
            for t in type_rows
        }
        type_info = {t["eq_type_id"]: t for t in type_rows}
        units, unit_ids, unit_serials = [], {}, {}
        for r in conn.execute("SELECT * FROM equipment_units ORDER BY serial_number"):
            t = type_info.get(r["eq_type_id"])
            if t is None:
                continue
            units.append(EquipmentUnit(
                r["serial_number"], type_keys[t["eq_type_id"]], t["sterilization_duration_mins"] or 0,
                bool(t["requires_sterilization"]), max(0, minute(r["available_at"])),
                r["current_status"] or "AVAILABLE", name=t["name"],
            ))
            unit_ids[r["serial_number"]] = r["unit_id"]
            unit_serials[r["unit_id"]] = r["serial_number"]

        beds, bed_ids, bed_codes = [], {}, {}
        for r in conn.execute("SELECT * FROM recovery_beds ORDER BY bed_code"):
            beds.append(RecoveryBed(r["bed_code"], r["bed_type"], max(0, minute(r["available_from"]))))
            bed_ids[r["bed_code"]] = r["bed_id"]
            bed_codes[r["bed_id"]] = r["bed_code"]

        synergy = {
            (str(r["surgeon_id"]), str(r["staff_id"])): float(r["synergy_score"] or 0)
            for r in conn.execute("SELECT * FROM team_synergy")
        }
        procedures = [
            {
                "id": str(r["procedure_id"]), "name": r["name"], "code": r["icd9_cm_code"],
                "duration": r["default_duration_mins"], "caps": r["required_room_capabilities"] or [],
                "icu": bool(r["requires_icu_post_op"]), "infection": r["infection_risk_level"] or "CLEAN",
            }
            for r in conn.execute("SELECT * FROM procedures ORDER BY name")
        ]
        cards = [
            {
                "surgeon_id": str(r["surgeon_id"]), "procedure_id": str(r["procedure_id"]),
                "duration": r["historical_avg_duration_mins"], "stddev": r["historical_stddev_mins"],
                "roles": r["required_staff_roles"] or {},
                "equipment": [type_keys[uuid.UUID(str(t))] for t in (r["required_equipment_types"] or [])
                              if uuid.UUID(str(t)) in type_keys],
                "notes": r["special_notes"],
            }
            for r in conn.execute("SELECT * FROM surgeon_preference_cards")
        ]

        recovery_sel = ", s.recovery_end_time" if has_recovery_col else ""
        rows = list(conn.execute(
            f"""SELECT s.schedule_id, s.request_id, s.or_id, s.reserved_bed_id, s.start_time, s.end_time,
                       s.cleanup_end_time, s.status, s.actual_start_time, s.actual_end_time,
                       s.ai_optimization_score, s.ai_reasoning_log{recovery_sel},
                       r.patient_hn, r.patient_name, r.primary_surgeon_id, r.procedure_id, r.urgency_level,
                       r.raw_nlp_input, p.name AS procedure_name,
                       COALESCE(p.required_room_capabilities, '[]'::jsonb) AS caps,
                       COALESCE(p.requires_icu_post_op, false) AS icu,
                       COALESCE(p.infection_risk_level, 'CLEAN') AS infection
                FROM scheduled_surgeries s
                JOIN surgery_requests r ON r.request_id = s.request_id
                LEFT JOIN procedures p ON p.procedure_id = r.procedure_id
                WHERE s.status IS DISTINCT FROM 'CANCELLED'
                  AND s.start_time < %(end)s AND s.cleanup_end_time > %(start)s
                ORDER BY s.start_time""",
            span,
        ))
        allocs: dict[uuid.UUID, list] = {}
        if rows:
            for a in conn.execute(
                "SELECT * FROM resource_allocations WHERE schedule_id = ANY(%s) ORDER BY allocated_from",
                ([r["schedule_id"] for r in rows],),
            ):
                allocs.setdefault(a["schedule_id"], []).append(a)
        # Rooms, equipment and beds held by bookings outside this day (e.g. an
        # ICU stay from yesterday, a tray still in CSSD, tomorrow's first case).
        outside = {**span, "far": end + timedelta(days=2), "ids": [r["schedule_id"] for r in rows]}
        held: dict[tuple[str, uuid.UUID], list[tuple[int, int]]] = {}
        for a in conn.execute(
            """SELECT a.equipment_unit_id AS id, a.allocated_from AS t0, a.allocated_until AS t1
               FROM resource_allocations a JOIN scheduled_surgeries s ON s.schedule_id = a.schedule_id
               WHERE a.equipment_unit_id IS NOT NULL AND s.status IS DISTINCT FROM 'CANCELLED'
                 AND a.allocated_until > %(start)s AND a.allocated_from < %(far)s
                 AND NOT (a.schedule_id = ANY(%(ids)s::uuid[]))""",
            outside,
        ):
            held.setdefault(("unit", a["id"]), []).append((minute(a["t0"]), minute(a["t1"])))
        for a in conn.execute(
            """SELECT or_id AS id, start_time AS t0, cleanup_end_time AS t1 FROM scheduled_surgeries
               WHERE status IS DISTINCT FROM 'CANCELLED' AND cleanup_end_time > %(start)s
                 AND start_time < %(far)s AND NOT (schedule_id = ANY(%(ids)s::uuid[]))""",
            outside,
        ):
            held.setdefault(("room", a["id"]), []).append((minute(a["t0"]), minute(a["t1"])))
        if has_recovery_col:
            for a in conn.execute(
                """SELECT reserved_bed_id AS id, end_time AS t0, recovery_end_time AS t1 FROM scheduled_surgeries
                   WHERE reserved_bed_id IS NOT NULL AND recovery_end_time IS NOT NULL
                     AND status IS DISTINCT FROM 'CANCELLED' AND recovery_end_time > %(start)s
                     AND end_time < %(far)s AND NOT (schedule_id = ANY(%(ids)s::uuid[]))""",
                outside,
            ):
                held.setdefault(("bed", a["id"]), []).append((minute(a["t0"]), minute(a["t1"])))
        for kind, objects, ids in (("unit", units, unit_ids), ("room", rooms, room_ids), ("bed", beds, bed_ids)):
            for obj in objects:
                code = getattr(obj, {"unit": "unit_id", "room": "or_id", "bed": "bed_id"}[kind])
                spans = held.get((kind, ids[code]))
                if spans:
                    obj.blocked = tuple(sorted(spans))

        staff_by_id = {s.staff_id: s for s in staff}
        unit_by_serial = {u.unit_id: u for u in units}
        assignments, cases, bookings, taken = {}, {}, {}, set()
        for r in rows:
            room = room_codes.get(r["or_id"])
            if room is None:
                continue
            cid = case_code(r["request_id"], r["urgency_level"], taken)
            taken.add(cid)
            surgeon = str(r["primary_surgeon_id"])
            start_m, end_m = minute(r["start_time"]), minute(r["end_time"])
            team, roles, unit_list, ready = [], Counter(), [], []
            for a in allocs.get(r["schedule_id"], []):
                if a["staff_id"] is not None and str(a["staff_id"]) != surgeon and str(a["staff_id"]) in staff_by_id:
                    team.append(str(a["staff_id"]))
                    roles[staff_by_id[str(a["staff_id"])].role] += 1
                elif a["equipment_unit_id"] is not None and a["equipment_unit_id"] in unit_serials:
                    serial = unit_serials[a["equipment_unit_id"]]
                    unit_list.append(serial)
                    ready.append((serial, minute(a["allocated_until"])))
            bed = bed_codes.get(r["reserved_bed_id"])
            rec_end_ts = r.get("recovery_end_time")
            recovery = None
            if bed is not None:
                recovery = max(0, minute(rec_end_ts) - end_m) if rec_end_ts else None
            infection = r["infection"] if r["infection"] in InfectionRisk.__members__ else "CLEAN"
            case = SurgeryCase(
                cid, r["patient_hn"], r["procedure_name"] or r["raw_nlp_input"] or "Surgery", surgeon,
                max(1, end_m - start_m),
                urgency=r["urgency_level"] if r["urgency_level"] in Urgency.__members__ else "ELECTIVE",
                required_capabilities=set(r["caps"]),
                required_staff_roles=dict(roles),
                required_equipment_types=[unit_by_serial[s].eq_type for s in unit_list],
                infection_risk=infection, requires_icu_post_op=bool(r["icu"]),
                recovery_mins=recovery, patient_name=r["patient_name"],
            )
            cases[cid] = case
            assignments[cid] = Assignment(
                cid, room, start_m, end_m, minute(r["cleanup_end_time"]), (surgeon, *team),
                tuple(unit_list), tuple(ready), bed,
                end_m if bed else None, end_m + case.recovery_duration if bed else None,
                frozen=start_m < now_min,
                reasoning=tuple(x for x in (r["ai_reasoning_log"] or "").split("\n") if x),
            )
            bookings[cid] = r

        result = ScheduleResult(
            scenario=f"Supabase {day.isoformat()}", status="LOADED", objective=None,
            assignments=assignments, unscheduled={}, cases=cases, staff_names=names,
            wall_time_s=0.0, now_min=max(0, min(now_min, HORIZON)),
        )
        hospital = Hospital(rooms, units, staff, beds, synergy)
        return World(
            day, start, result.now_min, hospital, room_ids, unit_ids, bed_ids,
            {key: tid for tid, key in type_keys.items()}, procedures, cards, result, bookings,
            has_recovery_col, has_activity, sorted(t for t in TABLES if not rls.get(t, False)), has_actor_col,
        )

    def activity(self, conn: psycopg.Connection, world: World, limit: int = 30) -> list[dict]:
        if not world.has_activity:
            return []
        return [
            {"kind": r["kind"], "title": r["title"], "log": r["agent_log"], "changes": r["changes"],
             "notifications": r["notifications"], "highlight": [], "created_at": r["created_at"].isoformat(),
             "actor": r.get("actor")}
            for r in conn.execute(
                "SELECT * FROM orchestra_activity WHERE planning_date = %s ORDER BY created_at DESC LIMIT %s",
                (world.day, limit),
            )
        ]

    # ----------------------------------------------------------------- write

    def save(
        self,
        conn: psycopg.Connection,
        world: World,
        after: ScheduleResult,
        new_requests: dict[str, dict],
        *,
        activity: dict | None = None,
    ) -> None:
        """Write ``after`` over ``world.result`` in the open transaction.

        Changed bookings are deleted and re-inserted with the same
        schedule_id (deletes first, so a ripple never trips the exclusion
        constraints half way). Rows that changed since they were loaded raise
        ``ConflictError``.
        """
        before = world.result.assignments
        changed = [cid for cid, a in after.assignments.items() if cid in before and _differs(before[cid], a)]
        removed = [cid for cid in before if cid not in after.assignments]
        try:
            for cid in changed + removed:
                row = world.bookings[cid]
                cur = conn.execute(
                    "DELETE FROM scheduled_surgeries WHERE schedule_id = %s AND or_id = %s "
                    "AND start_time = %s AND end_time = %s",
                    (row["schedule_id"], row["or_id"], row["start_time"], row["end_time"]),
                )
                if cur.rowcount != 1:
                    raise ConflictError(f"{cid} changed in the database while scheduling")
            for cid in changed:
                row = world.bookings[cid]
                a = after.assignments[cid]
                moved = (a.or_id, a.start_min) != (before[cid].or_id, before[cid].start_min)
                self._insert_schedule(conn, world, row["schedule_id"], row["request_id"], after.cases[cid], a, row)
                if moved:
                    conn.execute(
                        "UPDATE surgery_requests SET status = 'RESCHEDULED' WHERE request_id = %s", (row["request_id"],)
                    )
            for cid in removed:
                conn.execute(
                    "UPDATE surgery_requests SET status = 'PENDING' WHERE request_id = %s",
                    (world.bookings[cid]["request_id"],),
                )
            for cid, request in new_requests.items():
                a = after.assignments.get(cid)
                if a is None:
                    continue
                case = after.cases[cid]
                request_id = request["request_id"]
                conn.execute(
                    """INSERT INTO surgery_requests (request_id, patient_hn, patient_name, primary_surgeon_id,
                           procedure_id, urgency_level, raw_nlp_input, ai_predicted_duration_mins,
                           preferred_date_start, preferred_date_end, status)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'SCHEDULED')""",
                    (request_id, case.patient_hn, request["patient_name"], uuid.UUID(case.surgeon_id),
                     request.get("procedure_id"), case.urgency.value, request.get("raw_text"),
                     case.duration_mins, world.day, world.day),
                )
                self._insert_schedule(conn, world, uuid.uuid4(), request_id, case, a, None)
            if activity is not None:
                self.log_activity(conn, world, activity)
        except psycopg.errors.ExclusionViolation as exc:
            raise ConflictError("the database already holds an overlapping booking") from exc

    @staticmethod
    def log_activity(conn: psycopg.Connection, world: World, activity: dict) -> None:
        if not world.has_activity:
            return
        cols = ["planning_date", "kind", "title", "agent_log", "changes", "notifications"]
        values = [world.day, activity["kind"], activity["title"], Jsonb(activity["log"]),
                  Jsonb(activity["changes"]), Jsonb(activity["notifications"])]
        if world.has_actor_col:
            cols.append("actor")
            values.append(activity.get("actor"))
        conn.execute(
            f"INSERT INTO orchestra_activity ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})", values
        )

    @staticmethod
    def people(conn: psycopg.Connection) -> list[dict]:
        return [{"id": str(r["staff_id"]), "name": r["full_name"], "role": r["role"]}
                for r in conn.execute("SELECT staff_id, full_name, role FROM staff WHERE is_active ORDER BY full_name")]

    def _insert_schedule(self, conn, world: World, schedule_id, request_id, case: SurgeryCase,
                         a: Assignment, old: dict | None) -> None:
        cols = ["schedule_id", "request_id", "or_id", "reserved_bed_id", "start_time", "end_time",
                "cleanup_end_time", "actual_start_time", "actual_end_time", "ai_optimization_score",
                "ai_reasoning_log", "status"]
        reasoning = "\n".join(a.reasoning) if a.reasoning else (old or {}).get("ai_reasoning_log")
        values = [
            schedule_id, request_id, world.room_ids[a.or_id], world.bed_ids.get(a.bed_id) if a.bed_id else None,
            world.ts(a.start_min), world.ts(a.end_min), world.ts(a.cleanup_end_min),
            (old or {}).get("actual_start_time"), (old or {}).get("actual_end_time"),
            (old or {}).get("ai_optimization_score"), reasoning, (old or {}).get("status") or "CONFIRMED",
        ]
        if world.has_recovery_col:
            cols.append("recovery_end_time")
            values.append(world.ts(a.recovery_end_min) if a.recovery_end_min is not None else None)
        conn.execute(
            f"INSERT INTO scheduled_surgeries ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))})",
            values,
        )
        rows = [(schedule_id, "STAFF", uuid.UUID(sid), None, world.ts(a.start_min), world.ts(a.end_min))
                for sid in a.staff_ids]
        rows += [(schedule_id, "EQUIPMENT", None, world.unit_ids[serial], world.ts(a.start_min), world.ts(ready))
                 for serial, ready in a.equipment_ready_min]
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO resource_allocations
                       (schedule_id, resource_type, staff_id, equipment_unit_id, allocated_from, allocated_until)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                rows,
            )


def _differs(a: Assignment, b: Assignment) -> bool:
    return (
        (a.or_id, a.start_min, a.end_min, a.cleanup_end_min, a.bed_id, a.recovery_end_min)
        != (b.or_id, b.start_min, b.end_min, b.cleanup_end_min, b.bed_id, b.recovery_end_min)
        or set(a.staff_ids) != set(b.staff_ids)
        or set(a.equipment_ready_min) != set(b.equipment_ready_min)
    )
