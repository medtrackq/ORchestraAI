"""Supabase mode against a real PostgreSQL server.

Runs only when TEST_DATABASE_URL points at a PostgreSQL server where the user
may create databases (each test gets a fresh database with every migration):

    TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:54329/postgres pytest tests/test_db.py
"""
from __future__ import annotations

import os
import uuid
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"
pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")


@pytest.fixture()
def db_url():
    name = "orchestra_test_" + uuid.uuid4().hex[:10]
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(ADMIN_URL, dbname=name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                conn.execute(migration.read_text())
        yield url
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture()
def store(db_url):
    from app.db_store import DatabaseStore

    s = DatabaseStore(db_url, workers=2, time_limit_s=10)
    s.seed_demo()
    return s


def _tomorrow(store) -> str:
    return (store.db.today() + timedelta(days=1)).isoformat()


def _request(state, surgeon_name, procedure_name, hn, **extra):
    surgeon = next(s["id"] for s in state["options"]["surgeons"] if s["name"].startswith(surgeon_name))
    procedure = next(p for p in state["options"]["procedures"] if p["name"] == procedure_name)
    card = next(c for c in state["options"]["cards"]
                if c["surgeon_id"] == surgeon and c["procedure_id"] == procedure["id"])
    data = {
        "procedure": procedure["name"], "procedure_id": procedure["id"], "patient_hn": hn,
        "patient_name": f"Test {hn}", "surgeon_id": surgeon, "duration_mins": card["duration"],
        "urgency": "ELECTIVE", "required_capabilities": procedure["caps"],
        "required_equipment_types": card["equipment"], "required_staff_roles": card["roles"],
        "requires_icu_post_op": procedure["icu"], "infection_risk": procedure["infection"],
    }
    data.update(extra)
    return data


def _rows(url, sql, *args):
    with psycopg.connect(url) as conn:
        return conn.execute(sql, args).fetchall()


def test_seed_loads_a_clean_day(store, db_url):
    state = store.state()
    assert len(state["schedule"]["bookings"]) == 10
    assert state["warnings"] == [] and state["rls_off"] == [] and not state["empty"]
    assert _rows(db_url, "SELECT count(*) FROM scheduled_surgeries WHERE actual_start_time IS NULL")[0][0] == 10
    from app.db_store import DatabaseStore
    from app.store import StoreError

    with pytest.raises(StoreError):
        store.seed_demo()  # refuses to touch a database that has data


def test_booking_is_saved_and_reloaded(store, db_url):
    day = _tomorrow(store)
    state = store.state(day)
    assert state["schedule"]["bookings"] == []
    data = _request(state, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-T1")
    requests_before = _rows(db_url, "SELECT count(*) FROM surgery_requests")[0][0]
    preview = store.slot_check(data, "09:00", commit=False, day=day)
    assert preview["booked"] is False
    assert _rows(db_url, "SELECT count(*) FROM surgery_requests")[0][0] == requests_before  # preview writes nothing
    booked = store.slot_check(data, "09:00", commit=True, day=day)
    assert booked["booked"]
    assert _rows(db_url, "SELECT count(*) FROM surgery_requests")[0][0] == requests_before + 1

    from app.db_store import DatabaseStore

    fresh = DatabaseStore(db_url).state(day)  # new process view: read back from the database
    [b] = fresh["schedule"]["bookings"]
    assert b["start"] == "09:00" and b["patient_name"] == "Test HN-T1"
    assert fresh["warnings"] == []
    assert _rows(db_url, "SELECT status FROM surgery_requests WHERE patient_hn = 'HN-T1'")[0][0] == "SCHEDULED"
    kinds = {r[0] for r in _rows(db_url, "SELECT resource_type FROM resource_allocations a "
                                         "JOIN scheduled_surgeries s USING (schedule_id) "
                                         "JOIN surgery_requests r USING (request_id) WHERE r.patient_hn = 'HN-T1'")}
    assert kinds == {"STAFF", "EQUIPMENT"}
    assert fresh["activity"][0]["kind"] == "booking"


def test_emergency_and_overrun_ripple_are_persisted(store, db_url):
    day = _tomorrow(store)
    state = store.state(day)
    for hn, proc in (("HN-A", "Laparoscopic cholecystectomy"), ("HN-B", "Open inguinal hernia repair"),
                     ("HN-C", "Laparoscopic appendectomy")):
        assert store.slot_check(_request(state, "Dr.Napat", proc, hn), "08:00", commit=True, day=day)["booked"]
    before = {b["patient_hn"]: b for b in store.state(day)["schedule"]["bookings"]}

    er = _request(state, "Dr.Anan", "Emergency exploratory laparotomy", "HN-ER")
    result = store.emergency(er, "08:30", day=day)
    after_state = store.state(day)
    after = {b["patient_hn"]: b for b in after_state["schedule"]["bookings"]}
    assert "HN-ER" in after and after_state["warnings"] == []
    moved = [hn for hn in before if hn in after and after[hn]["start_min"] != before[hn]["start_min"]]
    for hn in moved:
        status = _rows(db_url, "SELECT status FROM surgery_requests WHERE patient_hn = %s", hn)[0][0]
        assert status == "RESCHEDULED"
    assert any(c["case_id"] == after["HN-ER"]["case_id"] for c in result["changes"])

    first = min(after.values(), key=lambda b: b["start_min"])
    store.overrun(first["case_id"], first["end_min"] - first["start_min"] + 30,
                  f"{(first['start_min'] + 10) // 60:02d}:{(first['start_min'] + 10) % 60:02d}", day=day)
    final = store.state(day)
    extended = next(b for b in final["schedule"]["bookings"] if b["patient_hn"] == first["patient_hn"])
    assert extended["end_min"] == first["end_min"] + 30
    assert final["warnings"] == []


def test_database_rejects_overlap_even_without_the_app(store, db_url):
    """A second writer with a stale view hits the exclusion constraint, not a silent double booking."""
    from app.db import ConflictError

    day = store.db.today() + timedelta(days=1)
    state = store.state(day.isoformat())
    data = _request(state, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-S1")
    with store.db.connect() as conn:
        stale = store.db.load(conn, day)
    assert store.slot_check(data, "09:00", commit=True, day=day.isoformat())["booked"]

    from app.store import build_case

    case = build_case(stale.hospital, {**data, "patient_hn": "HN-S2"}, "R-STALE1")
    check = store._optimizer(stale).check_slot(stale.result, case, 9 * 60, verbose=False)
    assert check.assignment is not None  # the stale view thinks 09:00 is free
    request = {"request_id": uuid.uuid4(), "patient_name": "Stale", "procedure_id": None, "raw_text": "x"}
    with pytest.raises(ConflictError):
        with store.db.connect() as conn:
            store.db.save(conn, stale, check.result, {"R-STALE1": request})
    assert _rows(db_url, "SELECT count(*) FROM surgery_requests WHERE patient_hn = 'HN-S2'")[0][0] == 0


def test_row_level_security_hides_patients_from_the_public_api_role(store, db_url):
    from app.auth import DbUserStore

    DbUserStore(store.db).create("someone", "Someone", "staff", None, "Temporary-Pass-77")
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DO $$ BEGIN CREATE ROLE anon NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$")
        conn.execute("GRANT USAGE ON SCHEMA public TO anon")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO anon")
        assert conn.execute("SELECT count(*) FROM surgery_requests").fetchone()[0] > 10
        conn.execute("SET ROLE anon")
        assert conn.execute("SELECT count(*) FROM surgery_requests").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM staff").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM app_users").fetchone()[0] == 0  # password hashes too


def test_accounts_are_stored_hashed_and_sessions_end_on_change(store, db_url):
    from app.auth import Accounts, AuthError, DbUserStore, Signer

    accounts = Accounts(DbUserStore(store.db), Signer(b"k" * 32))
    staff_id = next(p["id"] for p in store.people() if p["name"].startswith("Dr.Napat"))
    user = accounts.store.create("napat", "Napat", "surgeon", staff_id, "Temporary-Pass-77")
    assert user.must_change_password and user.staff_id == staff_id
    with pytest.raises(AuthError):
        accounts.store.create("Napat", "Again", "staff", None, "Temporary-Pass-77")  # user names are unique

    logged_in, token = accounts.login("NAPAT", "Temporary-Pass-77", "10.0.0.1")
    assert accounts.user_from_token(token).user_id == logged_in.user_id
    new_token = accounts.change_own_password(logged_in, "Temporary-Pass-77", "Real-Pass-2026!")
    assert accounts.user_from_token(token) is None  # the old session ended
    assert accounts.user_from_token(new_token) is not None
    stored, must_change = _rows(db_url, "SELECT password_hash, must_change_password FROM app_users")[0]
    assert stored.startswith("scrypt$") and "Real-Pass" not in stored and must_change is False

    accounts.store.update(logged_in.user_id, is_active=False)
    assert accounts.user_from_token(new_token) is None
    with pytest.raises(AuthError):
        accounts.login("napat", "Real-Pass-2026!", "10.0.0.1")


def test_actor_and_cssd_equipment_updates_are_saved(store, db_url):
    day = _tomorrow(store)
    state = store.state(day)
    scheduler = {"name": "Nurse A", "username": "a", "role": "scheduler", "staff_id": None}
    assert store.slot_check(_request(state, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-Q"), "09:00",
                            commit=True, day=day, actor=scheduler)["booked"]
    assert _rows(db_url, "SELECT actor FROM orchestra_activity WHERE kind = 'booking'")[0][0] == "Nurse A (scheduler)"

    booked = store.state(day)["schedule"]["bookings"][0]
    unit = booked["equipment"][0]["unit_id"]
    cssd = {"name": "CSSD B", "username": "b", "role": "cssd", "staff_id": None}
    result = store.update_equipment(unit, "STERILIZING", "13:00", day=day, actor=cssd)
    assert result["warnings"] and result["unit"]["ready_at"] == "13:00"
    assert _rows(db_url, "SELECT current_status FROM equipment_units WHERE serial_number = %s", unit)[0][0] == "STERILIZING"
    assert store.state(day)["warnings"]  # the scheduler now sees the conflict


def test_owner_login_still_works_before_migration_003(store, db_url):
    from app.auth import Accounts, AuthError, DbUserStore, Signer

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE app_users")
    accounts = Accounts(DbUserStore(store.db), Signer(b"k" * 32))
    assert accounts.store.by_username("anyone") is None
    with pytest.raises(AuthError, match="20261102000000_user_accounts.sql"):
        accounts.store.list()


def test_actual_times_and_history_live_in_supabase(store, db_url):
    from app.store import StoreError

    seeded = len(store.intake_context().history)
    assert seeded > 0  # seeding adds invented past cases with actual times
    assert _rows(db_url, "SELECT count(*) FROM surgery_requests WHERE patient_hn LIKE 'HIST-%%'")[0][0] == seeded
    with pytest.raises(StoreError):
        store.seed_history()  # refuses once real or demo history exists

    case = store.state()["schedule"]["bookings"][0]
    scheduler = {"name": "Nurse A", "username": "a", "role": "scheduler", "staff_id": None}
    result = store.record_actual(case["case_id"], "08:00", "09:40", actor=scheduler)
    assert result["actual_minutes"] == 100
    booking = next(b for b in store.state()["schedule"]["bookings"] if b["case_id"] == case["case_id"])
    assert (booking["actual_start"], booking["actual_end"]) == ("08:00", "09:40")
    assert len(store.intake_context().history) == seeded + 1
    assert _rows(db_url, "SELECT count(*) FROM orchestra_activity WHERE kind = 'actual'")[0][0] == 1

    outsider = {"name": "X", "username": "x", "role": "staff", "staff_id": "00000000-0000-0000-0000-000000000000"}
    with pytest.raises(StoreError):
        store.record_actual(case["case_id"], "08:00", None, actor=outsider)


def test_fatigue_hours_and_skills_come_from_the_database(store, db_url):
    today, tomorrow = store.state(), _tomorrow(store)
    worked: dict[str, int] = {}
    for b in today["schedule"]["bookings"]:
        for t in b["team"]:
            worked[t["staff_id"]] = worked.get(t["staff_id"], 0) + b["end_min"] - b["start_min"]
    staff = {p["staff_id"]: p for p in store.state(tomorrow)["staff"]}
    for sid, minutes in worked.items():
        assert staff[sid]["week_mins"] == minutes and staff[sid]["today_mins"] == 0  # yesterday counts, today empty
    assert all(p["daily_cap_mins"] == 600 and p["rest_mins"] == 480 for p in staff.values())

    robotic = next(p for p in today["options"]["procedures"] if p["name"].startswith("Robotic"))
    assert robotic["skills"] == {"SCRUB_NURSE": ["ROBOTIC_CERTIFIED"]}
    assert today["options"]["skills"]["SCRUB_NURSE"] == ["ROBOTIC_CERTIFIED"]
    # Migration 004 restores the robotic-nurse rule on robotic procedures that have none.
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("UPDATE procedures SET required_staff_skills = '{}'::jsonb")
        conn.execute((MIGRATIONS / "20261103000000_staff_fatigue_and_skills.sql").read_text())
    names = {r[0] for r in _rows(db_url, "SELECT name FROM procedures WHERE required_staff_skills <> '{}'::jsonb")}
    assert names == {p["name"] for p in today["options"]["procedures"] if "ROBOTIC_READY" in p["caps"]}


def test_late_case_needs_rest_before_the_next_day_booking(store, db_url):
    day1 = _tomorrow(store)
    day2 = (store.db.today() + timedelta(days=2)).isoformat()
    state = store.state(day2)
    morning = _request(state, "Dr.Anan", "Emergency appendectomy (open)", "HN-M", urgency="URGENT")
    assert store.slot_check(morning, "08:00", commit=True, day=day2)["booked"]
    anan = morning["surgeon_id"]
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("UPDATE staff SET min_rest_hours = 10 WHERE staff_id = %s", (anan,))

    late = _request(store.state(day1), "Dr.Anan", "Emergency appendectomy (open)", "HN-L", duration_mins=60)
    check = store.slot_check(late, "22:30", commit=False, day=day1)  # ends 23:30, 8.5 h before 08:00
    staffing = next(v for v in check["verdicts"] if v["agent"] == "Staffing Agent")
    assert not staffing["ok"] and "ได้พักไม่ถึง 10 ชม." in staffing["items"][0]["note"]
    assert not any(b["case_id"] == check["case_id"] for b in check["result"]["bookings"])

    result = store.emergency(late, "22:30", day=day1)  # an emergency may, with a warning
    issue = next(i for i in result["fatigue"] if i["staff_id"] == anan)
    assert issue["kind"] == "next_day" and issue["override"] and issue["minutes"] == 90
    assert any("ข้ามเกณฑ์ความเหนื่อยล้า" in line for line in result["log"])
    overrides = store.report(day1, day1)["fatigue_overrides"]
    assert overrides >= 1
    with psycopg.connect(db_url, autocommit=True) as conn:  # a line written before the messages were Thai
        conn.execute("""INSERT INTO orchestra_activity (planning_date, kind, title, agent_log, changes, notifications)
                        VALUES (%s, 'emergency', 'old', '["[Staffing Agent] FATIGUE OVERRIDE: x"]', '[]', '[]')""",
                     (day1,))
    assert store.report(day1, day1)["fatigue_overrides"] == overrides + 1
    saved = store.state(day1)
    assert saved["warnings"] == []  # an emergency override is not a conflict
    assert any(i["staff_id"] == anan and i["override"] for i in saved["fatigue"])
    next_day = store.state(day2)
    rested = {p["staff_id"]: p for p in next_day["staff"]}
    assert rested[anan]["rest_until"] == 23 * 60 + 30 + 10 * 60 - 24 * 60  # 09:30 on day 2
    # Day 2 now shows that the 08:00 case starts inside that rest, for the scheduler to fix.
    assert any(w.startswith("เกินเกณฑ์ความเหนื่อยล้า:") and "09:30" in w for w in next_day["warnings"])


def test_app_works_before_migration_004(db_url):
    """Deployed code may run before the fatigue/skills migration: defaults are used."""
    from app.db_store import DatabaseStore

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE staff DROP COLUMN max_hours_per_day, DROP COLUMN min_rest_hours")
        conn.execute("ALTER TABLE procedures DROP COLUMN required_staff_skills")
    store = DatabaseStore(db_url, workers=2, time_limit_s=10)
    store.seed_demo()
    state = store.state()
    assert len(state["schedule"]["bookings"]) == 10 and state["warnings"] == []
    assert all(p["daily_cap_mins"] == 600 and p["rest_mins"] == 480 for p in state["staff"])
    day = _tomorrow(store)
    data = _request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-OLD")
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
