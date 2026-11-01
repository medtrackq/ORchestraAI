"""PDPA: who was shown patients' identities, and removing them after the retention period.

Demo-store tests need no database; the rest run against a real PostgreSQL
server (TEST_DATABASE_URL).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import psycopg
import pytest

from app import privacy
from app.auth import User
from app.store import ScheduleStore, StoreError
from test_db import ADMIN_URL, _request, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
NURSE = User("u-nurse", "nurse.a", "Nurse A", "staff", staff_id="x")
SCHEDULER = User("u-sched", "sched.b", "Sched B", "scheduler")
ADMIN = {"name": "Admin A", "username": "a", "role": "admin", "staff_id": None, "user_id": "u-admin"}


# ------------------------------------------------------------- demo store

def test_same_view_is_logged_once_new_cases_and_printouts_again():
    s = ScheduleStore(workers=2, time_limit_s=10)
    log = privacy.AccessLog(s)
    state = s.state()
    assert log.record_state(NURSE, state) is True
    assert log.record_state(NURSE, state) is False  # same day, same cases, moments later
    assert log.record_state(SCHEDULER, state) is True  # another person
    more = {**state, "pending": [{"code": "R-NEW001"}]}
    assert log.record_state(NURSE, more) is True  # a case they had not been shown
    assert log.record_state(NURSE, state, "print") is True
    assert log.record_state(NURSE, state, "print") is True  # every printout counts
    assert log.record(NURSE, "schedule", "2026-11-02", []) is False  # nothing shown
    rows = log.entries()["entries"]
    assert [r["action"] for r in rows] == ["print", "print", "schedule", "schedule", "schedule"]
    assert rows[-1]["patients"] == len(state["schedule"]["bookings"])
    assert {r["username"] for r in log.entries(username="SCHED.B")["entries"]} == {"sched.b"}
    first = state["schedule"]["bookings"][0]
    by_hn = log.entries(hn=first["patient_hn"], user=SCHEDULER)["entries"]
    assert by_hn[0]["action"] == "lookup" and by_hn[0]["username"] == "sched.b"  # the lookup is recorded too
    assert all(first["case_id"] in r["case_codes"] for r in by_hn)
    assert log.entries(hn="NO-SUCH-HN")["hn_found"] is False
    with pytest.raises(StoreError, match="วันที่"):
        log.entries(date_from="2026-13-01")


def test_retention_needs_the_database():
    s = ScheduleStore(workers=2, time_limit_s=10)
    assert privacy.retention(s)["ready"] is False
    with pytest.raises(StoreError, match="Supabase"):
        privacy.set_retention(s, 365, None)
    assert privacy.RetentionTimer(s).tick() is None


# ------------------------------------------------------- Supabase store

def _book(store, day, hn, at):
    assert store.slot_check(_request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", hn), at,
                            commit=True, day=day)["booked"]
    return next(b["case_id"] for b in store.state(day)["schedule"]["bookings"] if b["patient_hn"] == hn)


def _age(db_url, hn, days):
    """Move a request (and its booking / cancellation) ``days`` into the past."""
    with psycopg.connect(db_url, autocommit=True) as conn:
        gap = f"{days} days"
        conn.execute("UPDATE surgery_requests SET created_at = created_at - %s::interval WHERE patient_hn = %s", (gap, hn))
        conn.execute("""UPDATE scheduled_surgeries s SET start_time = start_time - %s::interval,
                               end_time = end_time - %s::interval, cleanup_end_time = cleanup_end_time - %s::interval,
                               recovery_end_time = recovery_end_time - %s::interval
                        FROM surgery_requests r WHERE r.request_id = s.request_id AND r.patient_hn = %s""",
                     (gap, gap, gap, gap, hn))
        conn.execute("""UPDATE case_cancellations c SET cancelled_at = cancelled_at - %s::interval
                        FROM surgery_requests r WHERE r.request_id = c.request_id AND r.patient_hn = %s""", (gap, hn))


@needs_db
def test_access_is_recorded_in_the_database(store, db_url):
    log = privacy.AccessLog(store)
    day = _tomorrow(store)
    _book(store, day, "HN-A1", "09:00")
    assert log.record_state(NURSE, store.state(day), address="10.0.0.7") is True
    assert log.record_state(NURSE, store.state(day)) is False
    rows = _rows(db_url, "SELECT username, role, action, target_day::text, patients, address FROM patient_access_log")
    assert rows == [("nurse.a", "staff", "schedule", day, 1, "10.0.0.7")]
    assert _rows(db_url, "SELECT count(*) FROM patient_access_log WHERE 'HN-A1' = ANY(case_codes)") == [(0,)]  # codes only
    found = log.entries(hn="HN-A1", user=SCHEDULER)
    assert [(e["action"], e["username"]) for e in found["entries"]] == [("lookup", "sched.b"), ("schedule", "nurse.a")]
    assert log.entries(case=found["entries"][1]["case_codes"][0].lower(), username="nurse.a")["entries"][0]["patients"] == 1
    assert log.entries(date_from=day, date_to=day)["entries"] == []  # filters on when it was seen, not the schedule day


@needs_db
def test_identity_is_removed_after_the_retention_period(store, db_url):
    day = _tomorrow(store)
    old = _book(store, day, "HN-OLD", "09:00")
    store.readiness_update(old, {"item": "npo", "status": "PROBLEM", "note": "ญาติแจ้งว่าดื่มน้ำ"}, day=day, actor=ADMIN)
    gone = _book(store, day, "HN-GONE", "11:00")
    store.cancel_booking(gone, {"reason": "BED", "outcome": "CANCELLED", "note": "ผู้ป่วยขอเลื่อน"}, day=day, actor=ADMIN)
    _book(store, day, "HN-NEW", "13:00")
    waiting = _book(store, day, "HN-WAIT", "11:00")  # where HN-GONE was
    store.cancel_booking(waiting, {"reason": "NPO"}, day=day, actor=ADMIN)  # back on the waiting list
    for hn in ("HN-OLD", "HN-GONE", "HN-WAIT"):
        _age(db_url, hn, 400)
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("""INSERT INTO orchestra_activity (planning_date, kind, title, agent_log, changes, notifications)
                        VALUES (current_date - 400, 'emergency', 'old', '[]', '[]',
                                '[{"message": "[ORchestraAI] R-1 x (HN HN-OLD): moved"}]')""")
        conn.execute("""INSERT INTO patient_access_log (accessed_at, user_id, username, role, action)
                        VALUES (now() - interval '800 days', 'u', 'old.user', 'staff', 'schedule')""")

    assert privacy.retention(store)["patient_days"] is None
    first = privacy.apply(store)  # patient period not set: identities kept; the access log has its own period
    assert (first["cases"], first["activity_cleared"], first["access_pruned"]) == (0, 0, 1)
    with pytest.raises(StoreError, match="ระหว่าง 30"):
        privacy.set_retention(store, 7, None)
    settings = privacy.set_retention(store, 365, None, actor=ADMIN)
    assert (settings["patient_days"], settings["access_days"], settings["due"]) == (365, 730, 2)
    out = privacy.apply(store, actor=ADMIN)
    assert (out["cases"], out["activity_cleared"], out["access_pruned"]) == (2, 1, 0)
    assert out["notices_scrubbed"] >= 1  # the booking's notice is dated on the (moved) day, not 400 days ago

    rows = dict(_rows(db_url, """SELECT r.patient_name, r.patient_hn LIKE 'ANON-%%' FROM surgery_requests r
                                 WHERE r.anonymized_at IS NOT NULL"""))
    assert rows == {privacy.ANON_NAME: True}
    assert _rows(db_url, "SELECT count(*) FROM surgery_requests WHERE anonymized_at IS NOT NULL") == [(2,)]
    left = {r[0] for r in _rows(db_url, "SELECT patient_hn FROM surgery_requests WHERE anonymized_at IS NULL "
                                        "AND patient_name LIKE 'Test %%'")}
    assert left == {"HN-NEW", "HN-WAIT"}  # recent case and the waiting list keep their identity
    assert _rows(db_url, "SELECT count(*) FROM case_readiness WHERE note IS NOT NULL") == [(0,)]
    assert _rows(db_url, "SELECT note FROM case_cancellations WHERE reason = 'BED'") == [(None,)]
    assert _rows(db_url, "SELECT count(*) FROM orchestra_activity WHERE notifications::text LIKE '%%HN-OLD%%'") == [(0,)]
    assert _rows(db_url, "SELECT count(*) FROM patient_access_log WHERE username = 'old.user'") == [(0,)]
    titles = [t for (t,) in _rows(db_url, "SELECT title FROM orchestra_activity ORDER BY activity_id")]
    assert "ลบข้อมูลระบุตัวผู้ป่วยตามระยะเวลาเก็บ: 2 เคส" in titles
    long_ago = (store.db.today() - timedelta(days=399)).isoformat()
    assert store.report(long_ago, long_ago)["cases"] == 1  # the anonymised case still counts in reports
    assert privacy.apply(store)["cases"] == 0  # nothing left to do
    assert privacy.set_retention(store, None, 900)["patient_days"] is None  # switched off again


@needs_db
def test_the_nightly_run_happens_once_a_day(store, db_url):
    privacy.set_retention(store, 365, None)
    timer = privacy.RetentionTimer(store, hour=2)
    night = datetime.now(store.db.tz).replace(hour=1, minute=0)
    assert timer.tick(night) is None  # before 02:00
    first = timer.tick(night.replace(hour=3))
    assert first is not None and first["patient_days"] == 365
    assert timer.tick(night.replace(hour=4)) is None  # already ran today
    assert _rows(db_url, "SELECT count(*) FROM app_settings WHERE key LIKE 'retention_run:%%'") == [(1,)]


@needs_db
def test_works_before_migration_013(db_url):
    from app.db_store import DatabaseStore

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE patient_access_log")
        conn.execute("ALTER TABLE surgery_requests DROP COLUMN anonymized_at")
    s = DatabaseStore(db_url, workers=2, time_limit_s=10)
    s.seed_demo()
    log = privacy.AccessLog(s)
    assert log.record_state(NURSE, s.state()) is False and log.ready() is False
    assert log.entries()["ready"] is False
    assert privacy.retention(s)["ready"] is False
    with pytest.raises(StoreError, match="013"):
        privacy.set_retention(s, 365, None)
    assert privacy.RetentionTimer(s).tick(datetime.now(s.db.tz).replace(hour=3)) is None
    with psycopg.connect(db_url, autocommit=True) as conn:  # the migration runs while the app is up
        for path in sorted(privacy_migration()):
            conn.execute(path.read_text())
    assert log.record_state(NURSE, s.state()) is True  # the view before the table existed did not count
    assert privacy.retention(s)["ready"] is True


def privacy_migration():
    from app.system_status import MIGRATIONS

    return MIGRATIONS.glob("*_privacy.sql")
