"""Synthetic demo data for an empty Supabase database (explicit request only).

Writes the demo hospital from ``orchestra_engine.demo`` into the real
tables: rooms, equipment, beds, staff with shifts for the next two weeks,
team synergy, procedures and surgeon preference cards. All names and HNs
are invented.
"""
from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from orchestra_engine.demo import DEMO_SUPPLIES, build_hospital, elective_day
from orchestra_engine.intake import synthetic_history
from orchestra_engine.models import DEFAULT_TEAM, InfectionRisk

HISTORY_TAG = "synthetic history (demo)"

HEAVY = {"ROBOT_DA_VINCI", "C_ARM"}
NEEDS_BLOOD = {  # demo procedures whose checklist asks for blood to be reserved
    "Total knee arthroplasty (L)", "Total knee arthroplasty (R)", "Robotic radical prostatectomy",
    "Robotic partial nephrectomy", "Femoral-popliteal bypass", "Emergency exploratory laparotomy",
}
EMERGENCY_PROCEDURES = [
    # (name, surgeon code, minutes, equipment, infection, icu)
    ("Emergency exploratory laparotomy", "SUR-ONCALL", 120, ["MAJOR_GENERAL_TRAY"], "DIRTY", True),
    ("Emergency appendectomy (open)", "SUR-ONCALL", 60, ["MAJOR_GENERAL_TRAY"], "CONTAMINATED", False),
]


def is_empty(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM operating_rooms) AS rooms, (SELECT count(*) FROM staff) AS staff"
    ).fetchone()
    return row["rooms"] == 0 and row["staff"] == 0


def seed_resources(conn: psycopg.Connection, tz: ZoneInfo, today: date) -> dict:
    """Insert the demo hospital. Returns id maps for the demo bookings."""
    h = build_hospital()
    # Ready since midnight yesterday, so today's demo morning is bookable.
    ready = datetime(today.year, today.month, today.day, tzinfo=tz) - timedelta(days=1)
    for r in h.rooms:
        conn.execute(
            """INSERT INTO operating_rooms (room_code, room_type, capabilities, base_turnover_mins,
                   infectious_turnover_mins, status) VALUES (%s, %s, %s, %s, %s, %s)""",
            (r.or_id, r.room_type, Jsonb(sorted(r.capabilities)), r.base_turnover_mins,
             r.infectious_turnover_mins, r.status),
        )

    type_ids: dict[str, uuid.UUID] = {}
    for u in h.equipments:
        if u.eq_type not in type_ids:
            type_ids[u.eq_type] = conn.execute(
                """INSERT INTO equipment_types (name, category, requires_sterilization, sterilization_duration_mins)
                   VALUES (%s, %s, %s, %s) RETURNING eq_type_id""",
                (u.eq_type, "HEAVY_MACHINE" if u.eq_type in HEAVY else "INSTRUMENT_TRAY",
                 u.requires_sterilization, u.sterilization_mins),
            ).fetchone()["eq_type_id"]
        conn.execute(
            """INSERT INTO equipment_units (eq_type_id, serial_number, current_status, current_location, available_at)
               VALUES (%s, %s, %s, %s, %s)""",
            (type_ids[u.eq_type], u.unit_id, u.status, "CSSD_ZONE", ready),
        )

    for b in h.beds:
        conn.execute(
            "INSERT INTO recovery_beds (bed_code, bed_type, available_from) VALUES (%s, %s, %s)",
            (b.bed_id, b.bed_type, ready),
        )

    staff_ids: dict[str, uuid.UUID] = {}
    for s in h.staff:
        staff_ids[s.staff_id] = conn.execute(
            """INSERT INTO staff (employee_code, full_name, role, specialty, skills, max_hours_per_week,
                   current_weekly_hours, is_active) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING staff_id""",
            (s.staff_id, s.name, s.role, s.specialty, Jsonb(sorted(s.skills)), int(s.max_weekly_hours),
             s.current_weekly_hours, s.is_active),
        ).fetchone()["staff_id"]
        kind = "ON_CALL" if "on-call" in s.name.lower() else "REGULAR"
        rows = []
        for offset in range(-1, 14):
            day = today + timedelta(days=offset)
            midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
            rows += [(staff_ids[s.staff_id], midnight + timedelta(minutes=a), midnight + timedelta(minutes=b), kind)
                     for a, b in s.shifts]
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO staff_schedules (staff_id, shift_start, shift_end, shift_type) VALUES (%s, %s, %s, %s)",
                rows,
            )

    for (surgeon, member), score in h.team_synergy.items():
        conn.execute(
            "INSERT INTO team_synergy (surgeon_id, staff_id, synergy_score) VALUES (%s, %s, %s)",
            (staff_ids[surgeon], staff_ids[member], score),
        )

    procedure_ids: dict[str, uuid.UUID] = {}
    with_skills = _has_column(conn, "procedures", "required_staff_skills")

    def procedure(name, minutes, caps, icu, infection, skills=None) -> uuid.UUID:
        if name not in procedure_ids:
            cols = ["name", "default_duration_mins", "required_room_capabilities", "requires_icu_post_op",
                    "infection_risk_level"]
            values = [name, minutes, Jsonb(sorted(caps)), icu, infection]
            if with_skills:
                cols.append("required_staff_skills")
                values.append(Jsonb(skills or {}))
            if _has_column(conn, "procedures", "requires_blood"):
                cols.append("requires_blood")
                values.append(name in NEEDS_BLOOD)
            procedure_ids[name] = conn.execute(
                f"INSERT INTO procedures ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                "RETURNING procedure_id",
                values,
            ).fetchone()["procedure_id"]
        return procedure_ids[name]

    def card(surgeon_code, procedure_id, minutes, roles, equipment) -> None:
        conn.execute(
            """INSERT INTO surgeon_preference_cards (surgeon_id, procedure_id, historical_avg_duration_mins,
                   historical_stddev_mins, required_staff_roles, required_equipment_types)
               VALUES (%s, %s, %s, 15, %s, %s) ON CONFLICT (surgeon_id, procedure_id) DO NOTHING""",
            (staff_ids[surgeon_code], procedure_id, minutes, Jsonb(roles),
             Jsonb([str(type_ids[t]) for t in equipment])),
        )

    supply_ids: dict[str, uuid.UUID] = {}
    if _has_column(conn, "supply_items", "item_id"):
        for code, name, category, unit, stock in DEMO_SUPPLIES:
            supply_ids[code] = conn.execute(
                """INSERT INTO supply_items (code, name, category, unit, on_hand, reorder_level)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING item_id""",
                (code, name, category, unit, stock, 1),
            ).fetchone()["item_id"]

    for c in elective_day():
        pid = procedure(c.procedure, c.duration_mins, c.required_capabilities, c.requires_icu_post_op,
                        c.infection_risk.value, _case_skills(c))
        card(c.surgeon_id, pid, c.duration_mins, c.required_staff_roles, list(c.required_equipment_types))
        for code, qty in c.required_supplies.items():
            if code in supply_ids:
                conn.execute(
                    "INSERT INTO procedure_supplies (procedure_id, item_id, quantity) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING", (pid, supply_ids[code], qty))
    for name, surgeon, minutes, equipment, infection, icu in EMERGENCY_PROCEDURES:
        pid = procedure(name, minutes, [], icu, InfectionRisk(infection).value)
        card(surgeon, pid, minutes, dict(DEFAULT_TEAM), equipment)

    return {"staff": staff_ids, "procedures": procedure_ids}


def _has_column(conn: psycopg.Connection, table: str, column: str) -> bool:
    return conn.execute(
        """SELECT EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_schema = 'public' AND table_name = %s AND column_name = %s) AS e""",
        (table, column),
    ).fetchone()["e"]


def _case_skills(case) -> dict[str, list[str]]:
    return {role: sorted(skills) for role, skills in case.required_staff_skills.items() if skills}


def demo_catalog() -> tuple[list[dict], list[dict]]:
    """Procedures and preference cards of the demo hospital (demo / memory mode)."""
    procedures: dict[str, dict] = {}
    cards: dict[tuple[str, str], dict] = {}

    def procedure(name, minutes, caps, icu, infection, skills=None, supplies=None) -> str:
        pid = "P-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        procedures.setdefault(pid, {"id": pid, "name": name, "code": None, "duration": minutes,
                                    "caps": sorted(caps), "icu": icu, "infection": infection,
                                    "skills": skills or {}, "supplies": dict(supplies or {}),
                                    "blood": name in NEEDS_BLOOD})
        return pid

    for c in elective_day():
        pid = procedure(c.procedure, c.duration_mins, c.required_capabilities, c.requires_icu_post_op,
                        c.infection_risk.value, _case_skills(c), c.required_supplies)
        cards.setdefault((c.surgeon_id, pid), {
            "surgeon_id": c.surgeon_id, "procedure_id": pid, "duration": c.duration_mins, "stddev": 15,
            "roles": dict(c.required_staff_roles), "equipment": list(c.required_equipment_types), "notes": None})
    for name, surgeon, minutes, equipment, infection, icu in EMERGENCY_PROCEDURES:
        pid = procedure(name, minutes, [], icu, infection)
        cards.setdefault((surgeon, pid), {
            "surgeon_id": surgeon, "procedure_id": pid, "duration": minutes, "stddev": 15,
            "roles": dict(DEFAULT_TEAM), "equipment": list(equipment), "notes": None})
    return list(procedures.values()), list(cards.values())


def has_history(conn: psycopg.Connection) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM scheduled_surgeries WHERE actual_end_time IS NOT NULL) AS e"
    ).fetchone()["e"]


def seed_history(conn: psycopg.Connection, world, *, days: int = 40) -> int:
    """Insert invented past cases with actual times so the duration forecast has
    something to learn from. Rows are tagged and use HN "HIST-...". Returns the count."""
    procs = {p["id"]: p for p in world.procedures}
    rooms = world.hospital.rooms
    if not rooms:
        return 0
    tz = world.start.tzinfo
    next_free: dict[tuple[date, str], int] = {}
    count = 0
    for rec in synthetic_history(world.cards, days=days):
        proc = procs.get(rec["procedure_id"])
        if proc is None:
            continue
        room = next((r for r in rooms if r.supports(frozenset(proc["caps"]))), None)
        if room is None:
            continue
        day = world.day - timedelta(days=rec["days_ago"])
        start = next_free.get((day, room.or_id), 8 * 60)
        end, clean = start + rec["planned"], start + rec["planned"] + room.base_turnover_mins
        if clean > 20 * 60:
            continue
        next_free[(day, room.or_id)] = clean + max(0, rec["actual"] - rec["planned"])
        midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
        at = lambda m: midnight + timedelta(minutes=m)  # noqa: E731
        count += 1
        request_id = uuid.uuid4()
        conn.execute(
            """INSERT INTO surgery_requests (request_id, patient_hn, patient_name, primary_surgeon_id, procedure_id,
                   urgency_level, raw_nlp_input, ai_predicted_duration_mins, preferred_date_start,
                   preferred_date_end, status)
               VALUES (%s, %s, %s, %s, %s, 'ELECTIVE', %s, %s, %s, %s, 'SCHEDULED')""",
            (request_id, f"HIST-{count:04d}", "ประวัติตัวอย่าง", uuid.UUID(rec["surgeon_id"]),
             uuid.UUID(rec["procedure_id"]), HISTORY_TAG, rec["planned"], day, day),
        )
        conn.execute(
            """INSERT INTO scheduled_surgeries (request_id, or_id, start_time, end_time, cleanup_end_time,
                   actual_start_time, actual_end_time, ai_reasoning_log, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'COMPLETED')""",
            (request_id, world.room_ids[room.or_id], at(start), at(end), at(clean), at(start),
             at(start + rec["actual"]), HISTORY_TAG),
        )
    return count
