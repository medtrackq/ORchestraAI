"""Hospital data pages (Supabase mode) against a real PostgreSQL server:
rooms, closures, staff, shifts and leave, equipment, and re-planning a day."""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from test_db import ADMIN_URL, _request, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
ADMIN = {"name": "Admin A", "username": "a", "role": "admin", "staff_id": None}


@pytest.fixture()
def admin(store):
    from app.admin_store import HospitalAdmin

    return HospitalAdmin(store)


def test_rooms_staff_and_equipment_are_edited(admin, store, db_url):
    from app.store import StoreError

    admin.save_room({"code": "OR-9", "type": "NEGATIVE_PRESSURE", "capabilities": ["laminar_flow"],
                     "base_turnover_mins": 40, "infectious_turnover_mins": 90}, actor=ADMIN)
    room = next(r for r in admin.rooms()["rooms"] if r["code"] == "OR-9")
    assert room["capabilities"] == ["LAMINAR_FLOW"] and room["infectious_turnover_mins"] == 90
    with pytest.raises(StoreError, match="มีห้อง OR-9"):
        admin.save_room({"code": "OR-9"})
    admin.save_room({**room, "status": "MAINTENANCE"}, code="OR-9")
    rooms = {r["id"]: r for r in store.state()["options"]["rooms"]}
    assert rooms["OR-9"]["status"] == "MAINTENANCE" and not rooms["OR-9"]["usable"]

    new = admin.save_staff({"code": "N-100", "name": "Nurse New", "role": "SCRUB_NURSE", "skills": ["robotic_certified"],
                            "max_hours_per_week": 40, "max_hours_per_day": 8, "min_rest_hours": 11}, actor=ADMIN)
    person = next(p for p in admin.staff()["staff"] if p["id"] == new["id"])
    assert person["skills"] == ["ROBOTIC_CERTIFIED"] and person["max_hours_per_day"] == 8 and person["min_rest_hours"] == 11
    with pytest.raises(StoreError, match="N-100"):
        admin.save_staff({"code": "N-100", "name": "Twin", "role": "SCRUB_NURSE"})
    with pytest.raises(StoreError):
        admin.save_staff({"code": "N-101", "name": "Bad", "role": "JANITOR"})
    admin.save_staff({**person, "active": False}, staff_id=new["id"])
    assert not next(p for p in admin.staff()["staff"] if p["id"] == new["id"])["active"]

    kind = admin.save_type({"name": "Cardiac tray", "category": "INSTRUMENT_TRAY", "sterilization_mins": 150})
    admin.save_unit({"serial": "CARD-01", "type_id": kind["id"], "location": "CSSD"})
    with pytest.raises(StoreError, match="CARD-01"):
        admin.save_unit({"serial": "CARD-01", "type_id": kind["id"]})
    with pytest.raises(StoreError, match="cardiac TRAY"):
        admin.save_type({"name": "cardiac TRAY"})  # names are unique, ignoring case
    assert {"type": "Cardiac tray", "units": 1} in store.state()["options"]["equipment_types"]
    titles = {r[0] for r in _rows(db_url, "SELECT title FROM orchestra_activity WHERE kind = 'admin'")}
    assert "เพิ่มห้อง OR-9" in titles and "เพิ่มบุคลากร Nurse New" in titles


def test_leave_is_reported_and_replanning_fixes_the_day(admin, store, db_url):
    day = _tomorrow(store)
    state = store.state(day)
    data = _request(state, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-LV")
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    booking = store.state(day)["schedule"]["bookings"][0]
    nurse = next(t for t in booking["team"] if t["staff_id"] != data["surgeon_id"])

    result = admin.add_shifts({"staff_ids": [nurse["staff_id"]], "date_from": day, "date_to": day,
                               "start": "00:00", "end": "00:00", "type": "LEAVE", "note": "ลาป่วย"}, actor=ADMIN)
    assert result["created"] == 1
    [affected] = result["affected"]
    assert affected["date"] == day and any("off shift" in p for p in affected["problems"])
    listed = admin.shifts(day, day, nurse["staff_id"])["shifts"]
    assert any(s["type"] == "LEAVE" and s["note"] == "ลาป่วย" for s in listed)

    fixed = store.replan(day, actor=ADMIN)
    after = store.state(day)
    assert after["warnings"] == [] and fixed["changes"]
    assert nurse["staff_id"] not in [t["staff_id"] for t in after["schedule"]["bookings"][0]["team"]]
    assert _rows(db_url, "SELECT count(*) FROM orchestra_activity WHERE kind = 'replan'")[0][0] == 1

    leave = next(s for s in listed if s["type"] == "LEAVE")
    assert admin.delete_shifts({"ids": [leave["id"]]})["deleted"] == 1


def test_room_closure_moves_cases_on_replan(admin, store):
    day = _tomorrow(store)
    data = _request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-CL")
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    room = store.state(day)["schedule"]["bookings"][0]["or_id"]
    result = admin.add_closure({"room": room, "start_date": day, "start_time": "07:00", "end_date": day,
                                "end_time": "13:00", "reason": "ซ่อมแอร์"}, actor=ADMIN)
    assert result["affected"] and "ซ่อมแอร์" in " ".join(result["affected"][0]["problems"])
    state = store.state(day)
    closed = next(r for r in state["options"]["rooms"] if r["id"] == room)
    assert closed["closures"] == [{"start_min": 7 * 60, "end_min": 13 * 60, "reason": "ซ่อมแอร์"}]

    store.replan(day)
    moved = store.state(day)
    assert moved["warnings"] == []
    b = moved["schedule"]["bookings"][0]
    assert b["or_id"] != room or b["start_min"] >= 13 * 60
    assert admin.delete_closure(result["id"])["deleted"] == result["id"]
    assert admin.rooms()["closures"] == []


def test_overnight_on_call_and_airborne_case(admin, store):
    day = _tomorrow(store)
    anan = next(s["id"] for s in store.state(day)["options"]["surgeons"] if s["name"].startswith("Dr.Anan"))
    admin.add_shifts({"staff_ids": [anan], "date_from": day, "date_to": day, "start": "20:00", "end": "08:00",
                      "type": "ON_CALL"})
    [night] = [s for s in admin.shifts(day, day, anan)["shifts"] if s["start"] == f"{day} 20:00"]
    assert night["type"] == "ON_CALL" and night["end"].endswith("08:00")
    assert night["end"][:10] > day  # ends the next morning

    room = next(r for r in admin.rooms()["rooms"] if r["code"] == "OR-2")
    assert "NEGATIVE_PRESSURE" in room["capabilities"]  # the demo hospital's isolation room
    admin.save_room({**room, "capabilities": []}, code="OR-2")
    data = _request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-TB", airborne=True)
    check = store.slot_check(data, "09:00", commit=False, day=day)
    assert "NEGATIVE_PRESSURE" in " ".join(check["result"]["unscheduled"][0]["reasons"])  # no such room now
    admin.save_room(room, code="OR-2")
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    [b] = store.state(day)["schedule"]["bookings"]
    assert b["or_id"] == "OR-2" and b["airborne"]
    assert b["cleanup_end_min"] - b["end_min"] == 60  # infectious turnover
    assert store.state(day)["warnings"] == []


def test_demo_mode_refuses_hospital_data():
    from app.admin_store import require_supabase
    from app.store import ScheduleStore, StoreError

    with pytest.raises(StoreError, match="Supabase"):
        require_supabase(ScheduleStore())


def test_case_that_no_longer_fits_waits_for_a_new_day(admin, store, db_url):
    day = _tomorrow(store)
    later = (store.db.today() + timedelta(days=2)).isoformat()
    data = _request(store.state(day), "Dr.Kittipong", "Robotic partial nephrectomy", "HN-W1")
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    admin.add_closure({"room": "OR-3", "start_date": day, "start_time": "00:00", "end_date": later,
                       "end_time": "00:00", "reason": "robot service"})  # the only robotic room
    result = store.replan(day)
    assert [u["case_id"] for u in result["unscheduled"]] and store.state(day)["schedule"]["bookings"] == []
    [waiting] = store.state(later)["pending"]  # visible on every day until rebooked
    assert waiting["patient_hn"] == "HN-W1" and waiting["was_on"] == day
    request_id = waiting["id"]

    rebook = {**data, "pending_id": request_id}
    assert store.slot_check(rebook, "09:00", commit=True, day=later)["booked"]
    assert store.state(later)["pending"] == []
    [b] = store.state(later)["schedule"]["bookings"]
    assert b["case_id"] == waiting["code"]  # same request, same code
    assert _rows(db_url, "SELECT count(*), max(status) FROM surgery_requests WHERE patient_hn = 'HN-W1'")[0] == (1, "SCHEDULED")

    from app.store import StoreError

    with pytest.raises(StoreError):
        store.slot_check(rebook, "13:00", commit=True, day=later)  # already booked


def test_waiting_request_can_be_cancelled(admin, store, db_url):
    day = _tomorrow(store)
    data = _request(store.state(day), "Dr.Kittipong", "Robotic partial nephrectomy", "HN-W2")
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    admin.add_closure({"room": "OR-3", "start_date": day, "end_date": day, "start_time": "00:00", "end_time": "23:59"})
    store.replan(day)
    [waiting] = store.state(day)["pending"]
    other = {"name": "Dr.X", "username": "x", "role": "surgeon", "staff_id": str(uuid4())}
    from app.store import Forbidden

    with pytest.raises(Forbidden):
        store.cancel_pending(waiting["id"], actor=other)
    store.cancel_pending(waiting["id"])
    assert store.state(day)["pending"] == []
    assert _rows(db_url, "SELECT status FROM surgery_requests WHERE patient_hn = 'HN-W2'")[0][0] == "CANCELLED"


def test_app_works_before_migration_005(db_url):
    import psycopg

    from app.admin_store import HospitalAdmin
    from app.db_store import DatabaseStore
    from app.store import StoreError

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE room_closures")
        conn.execute("ALTER TABLE surgery_requests DROP COLUMN airborne_isolation")
        conn.execute("ALTER TABLE staff_schedules DROP COLUMN note")
    store = DatabaseStore(db_url, workers=2, time_limit_s=10)
    store.seed_demo()
    state = store.state()
    assert len(state["schedule"]["bookings"]) == 10 and state["warnings"] == [] and state["rls_off"] == []
    admin = HospitalAdmin(store)
    assert admin.rooms()["closures_ready"] is False
    day = _tomorrow(store)
    data = _request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-OLD5", airborne=True)
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    nurse = next(p["id"] for p in admin.staff()["staff"] if p["role"] == "SCRUB_NURSE")
    assert admin.add_shifts({"staff_ids": [nurse], "date_from": day, "date_to": day, "type": "LEAVE",
                             "start": "00:00", "end": "00:00", "note": "x"})["created"] == 1
    with pytest.raises(StoreError, match="20261104000000"):
        admin.add_closure({"room": "OR-1", "start_date": day, "end_date": day, "end_time": "12:00"})
