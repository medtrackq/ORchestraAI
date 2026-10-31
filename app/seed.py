"""Synthetic demo data for an empty Supabase database (explicit request only).

Writes the demo hospital from ``orchestra_engine.demo`` into the real
tables: rooms, equipment, beds, staff with shifts for the next two weeks,
team synergy, procedures and surgeon preference cards. All names and HNs
are invented.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg
from psycopg.types.json import Jsonb

from orchestra_engine.demo import build_hospital, elective_day
from orchestra_engine.models import DEFAULT_TEAM, InfectionRisk

HEAVY = {"ROBOT_DA_VINCI", "C_ARM"}
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

    def procedure(name, minutes, caps, icu, infection) -> uuid.UUID:
        if name not in procedure_ids:
            procedure_ids[name] = conn.execute(
                """INSERT INTO procedures (name, default_duration_mins, required_room_capabilities,
                       requires_icu_post_op, infection_risk_level) VALUES (%s, %s, %s, %s, %s) RETURNING procedure_id""",
                (name, minutes, Jsonb(sorted(caps)), icu, infection),
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

    for c in elective_day():
        pid = procedure(c.procedure, c.duration_mins, c.required_capabilities, c.requires_icu_post_op,
                        c.infection_risk.value)
        card(c.surgeon_id, pid, c.duration_mins, c.required_staff_roles, list(c.required_equipment_types))
    for name, surgeon, minutes, equipment, infection, icu in EMERGENCY_PROCEDURES:
        pid = procedure(name, minutes, [], icu, InfectionRisk(infection).value)
        card(surgeon, pid, minutes, dict(DEFAULT_TEAM), equipment)

    return {"staff": staff_ids, "procedures": procedure_ids}
