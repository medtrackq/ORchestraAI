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
    from app.admin_store import Inventory

    return Inventory(store)


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


# ------------------------------------------------------------ supplies and implants

def _stock(state, code):
    return next(s for s in state["options"]["supplies"] if s["id"] == code)


def test_bookings_reserve_supplies_and_deliveries_free_them(admin, store, db_url):
    today = store.state()
    drape = _stock(today, "ROBO-DRAPE")
    assert (drape["available"], drape["booked"], drape["left"]) == (2, 2, 0)  # both demo robotic cases hold one
    robotic = next(p for p in today["options"]["procedures"] if p["name"] == "Robotic partial nephrectomy")
    assert robotic["supplies"] == {"ROBO-DRAPE": 1}

    day = _tomorrow(store)
    data = _request(store.state(day), "Dr.Kittipong", "Robotic partial nephrectomy", "HN-S1",
                    required_supplies={"ROBO-DRAPE": 1})
    check = store.slot_check(data, "09:00", commit=False, day=day)
    equipment = next(v for v in check["verdicts"] if v["agent"] == "Equipment & CSSD Agent")
    assert not equipment["ok"] and "ยังไม่ได้สั่งของเพิ่ม" in equipment["items"][-1]["note"]
    assert "Robotic drape kit" in check["result"]["unscheduled"][0]["reasons"][0]

    order = admin.expect_delivery("ROBO-DRAPE", {"quantity": 3, "expected_date": day}, actor=ADMIN)
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    assert _rows(db_url, """SELECT r.quantity, r.status FROM supply_reservations r
                            JOIN surgery_requests q USING (request_id) WHERE q.patient_hn = 'HN-S1'""") == [(1, "RESERVED")]
    item = next(i for i in admin.inventory()["items"] if i["code"] == "ROBO-DRAPE")
    # Today's two cases use the two on the shelf; the three arriving tomorrow cover tomorrow's case.
    assert (item["on_hand"], item["reserved"], item["free"], item["free_later"]) == (2, 3, 0, 2)

    # Cancelling the order leaves tomorrow's case without a drape kit: reported for re-planning.
    result = admin.cancel_delivery(order["id"])
    assert result["affected"][0]["date"] == day and "Robotic drape kit" in result["affected"][0]["problems"][0]
    admin.receive("ROBO-DRAPE", {"quantity": 5, "note": "ซื้อด่วน"})
    assert store.state(day)["warnings"] == []


def test_ended_cases_use_up_stock_and_counts_are_set(admin, store, db_url):
    with __import__("psycopg").connect(db_url, autocommit=True) as conn:
        conn.execute("""UPDATE scheduled_surgeries s SET actual_start_time = NOW() - INTERVAL '3 hours',
                            actual_end_time = NOW() - INTERVAL '1 minute'
                        FROM surgery_requests r WHERE r.request_id = s.request_id AND r.patient_hn = 'HN-0010'""")
    before = _stock(store.state(), "VASC-GRAFT")
    item = next(i for i in admin.inventory()["items"] if i["code"] == "VASC-GRAFT")  # settles
    assert item["on_hand"] == 0 and item["reserved"] == 0
    assert _rows(db_url, "SELECT status FROM supply_reservations r JOIN supply_items i USING (item_id) "
                         "WHERE i.code = 'VASC-GRAFT'") == [("USED",)]
    assert _stock(store.state(), "VASC-GRAFT")["left"] == before["left"]  # what is free did not change

    result = admin.count_stock("KNEE-IMP", {"on_hand": 1, "note": "นับสต็อกสิ้นเดือน"})
    assert result["on_hand"] == 1 and result["affected"]  # two knee cases today hold two
    admin.set_procedure_supplies(next(p["id"] for p in admin.inventory()["procedures"]
                                      if p["name"] == "Open inguinal hernia repair"), {"supplies": {"MESH-15": 2}})
    hernia = next(p for p in store.state()["options"]["procedures"] if p["name"] == "Open inguinal hernia repair")
    assert hernia["supplies"] == {"MESH-15": 2}


def test_supply_items_are_validated(admin):
    from app.store import StoreError

    admin.save_item({"code": "STENT-3", "name": "Coronary stent 3 mm", "category": "IMPLANT", "on_hand": 4,
                     "reorder_level": 2})
    with pytest.raises(StoreError, match="STENT-3"):
        admin.save_item({"code": "STENT-3", "name": "Again"})
    with pytest.raises(StoreError):
        admin.expect_delivery("STENT-3", {"quantity": 2, "expected_date": "2000-01-01"})
    admin.save_item({"name": "Coronary stent 3 mm", "category": "IMPLANT", "reorder_level": 5}, code="STENT-3")
    stent = next(i for i in admin.inventory()["items"] if i["code"] == "STENT-3")
    assert stent["low"] and stent["free"] == 4


def test_app_works_before_migration_006(db_url):
    import psycopg

    from app.db_store import DatabaseStore

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE supply_reservations, procedure_supplies, supply_deliveries, supply_items")
    store = DatabaseStore(db_url, workers=2, time_limit_s=10)
    store.seed_demo()
    state = store.state()
    assert len(state["schedule"]["bookings"]) == 10 and state["warnings"] == [] and state["options"]["supplies"] == []
    from app.admin_store import Inventory

    assert Inventory(store).inventory()["ready"] is False


# ------------------------------------------------ OR Facility and Equipment & CSSD agents

def test_elective_hours_per_room(admin, store):
    for room in admin.rooms()["rooms"]:
        admin.save_room({**room, "elective_start": "08:00", "elective_end": "12:00"}, code=room["code"])
    assert admin.rooms()["rooms"][0]["elective_start"] == "08:00"
    day = _tomorrow(store)
    state = store.state(day)
    assert state["options"]["rooms"][0]["elective"] == [8 * 60, 12 * 60]
    data = _request(state, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-H1")
    check = store.slot_check(data, "13:00", commit=False, day=day)
    facility = next(v for v in check["verdicts"] if v["agent"] == "OR Facility Agent")
    assert not facility["ok"] and "elective hours are 08:00-12:00" in facility["items"][0]["note"]
    assert "elective hours" in check["result"]["unscheduled"][-1]["reasons"][0]
    assert store.slot_check({**data, "urgency": "URGENT"}, "13:00", commit=True, day=day)["booked"]


def test_planned_equipment_maintenance(admin, store):
    day = _tomorrow(store)
    result = admin.add_maintenance({"serial": "DV-01", "start_date": day, "start_time": "07:00", "end_date": day,
                                    "end_time": "13:00", "reason": "PM ประจำปี"}, actor=ADMIN)
    assert result["affected"] == []
    assert admin.equipment()["maintenance"][0]["reason"] == "PM ประจำปี"
    data = _request(store.state(day), "Dr.Kittipong", "Robotic partial nephrectomy", "HN-M1",
                    required_supplies={})
    check = store.slot_check(data, "09:00", commit=False, day=day)
    equipment = next(v for v in check["verdicts"] if v["agent"] == "Equipment & CSSD Agent")
    assert any("planned maintenance 07:00-13:00 (PM ประจำปี)" in i["note"] for i in equipment["items"])
    today = store.db.today().isoformat()
    clash = admin.add_maintenance({"serial": "DV-01", "start_date": today, "end_date": today, "start_time": "00:00",
                                   "end_time": "23:59", "reason": "เสีย"})  # today's robotic cases need it
    assert clash["affected"][0]["date"] == today and "planned maintenance" in clash["affected"][0]["problems"][0]
    admin.delete_maintenance(clash["id"])
    assert store.state()["warnings"] == []


def test_recovery_beds_are_managed(admin, store):
    day = _tomorrow(store)
    for bed in ("ICU-1", "ICU-2"):
        admin.occupy_bed(bed, {"until_date": day, "until_time": "23:00", "note": "ผู้ป่วยอายุรกรรม"})
    data = _request(store.state(day), "Dr.Pimchanok", "Femoral-popliteal bypass", "HN-B1", required_supplies={})
    check = store.slot_check(data, "08:00", commit=False, day=day)
    assert "ICU_SURGICAL bed" in check["result"]["unscheduled"][-1]["reasons"][0]
    beds = {b["code"]: b for b in admin.beds()["beds"]}
    assert beds["ICU-1"]["occupied_until"] == f"{day} 23:00"
    admin.occupy_bed("ICU-2", {})  # free now
    admin.save_bed({"code": "ICU-3", "type": "ICU_SURGICAL"})
    admin.save_bed({"type": "ICU_SURGICAL", "status": "CLOSED", "note": "ปิดปรับปรุง"}, code="ICU-3")
    assert store.slot_check(data, "08:00", commit=True, day=day)["booked"]
    state = store.state(day)
    [b] = state["schedule"]["bookings"]
    assert b["bed_id"] == "ICU-2"
    closed = next(x for x in state["beds"] if x["id"] == "ICU-3")
    assert closed["status"] == "CLOSED" and not closed["usable"]


def test_procedures_and_recovery_time(admin, store):
    from app.store import StoreError

    made = admin.save_procedure({"name": "Thyroidectomy", "code": "06.4", "duration": 150, "caps": ["laminar_flow"],
                                 "infection": "CLEAN", "recovery_mins": 120,
                                 "skills": {"SCRUB_NURSE": ["head_neck"]}})
    proc = next(p for p in admin.procedures()["procedures"] if p["id"] == made["id"])
    assert (proc["recovery_mins"], proc["caps"], proc["skills"]) == (120, ["LAMINAR_FLOW"], {"SCRUB_NURSE": ["HEAD_NECK"]})
    with pytest.raises(StoreError, match="06.4"):
        admin.save_procedure({"name": "Other", "code": "06.4"})
    admin.save_procedure({**proc, "skills": {}}, procedure_id=made["id"])
    day = _tomorrow(store)
    state = store.state(day)
    assert next(p for p in state["options"]["procedures"] if p["id"] == made["id"])["recovery_mins"] == 120
    surgeon = next(s["id"] for s in state["options"]["surgeons"] if s["name"].startswith("Dr.Napat"))
    data = {"procedure": "Thyroidectomy", "procedure_id": made["id"], "patient_hn": "HN-T9", "patient_name": "T",
            "surgeon_id": surgeon, "duration_mins": 150, "required_capabilities": ["LAMINAR_FLOW"],
            "recovery_mins": 120}
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    [b] = store.state(day)["schedule"]["bookings"]  # read back from the database
    assert b["recovery_end_min"] - b["recovery_start_min"] == 120


def test_cssd_queue_is_in_the_state(store):
    jobs = store.state()["cssd"]
    assert jobs and {"unit_id", "ready", "next_case", "slack_mins", "level"} <= set(jobs[0])
    assert all(j["reprocess_mins"] > 0 for j in jobs)


def test_app_works_before_migration_007(db_url):
    import psycopg

    from app.db_store import DatabaseStore

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE equipment_maintenance")
        conn.execute("ALTER TABLE operating_rooms DROP COLUMN elective_start, DROP COLUMN elective_end")
        conn.execute("ALTER TABLE recovery_beds DROP COLUMN status, DROP COLUMN note")
        conn.execute("ALTER TABLE procedures DROP COLUMN recovery_mins")
    store = DatabaseStore(db_url, workers=2, time_limit_s=10)
    store.seed_demo()
    state = store.state()
    assert len(state["schedule"]["bookings"]) == 10 and state["warnings"] == [] and state["rls_off"] == []
    from app.admin_store import Inventory

    admin = Inventory(store)
    assert admin.equipment()["maintenance_ready"] is False and admin.beds()["status_ready"] is False
    room = admin.rooms()["rooms"][0]
    admin.save_room({**room, "elective_start": "08:00", "elective_end": "16:00"}, code=room["code"])  # ignored


# ------------------------------------------------------------------ management reports

def test_management_report_from_the_database(store, db_url):
    report = store.report()
    assert report["cases"] >= 10 and report["cases_timed"] > 0 and report["demo_cases"] > 0  # demo history counts
    assert report["kpi"]["utilization"] is not None and report["kpi"]["duration_mae"] is not None
    assert report["by_surgeon"] and report["per_day"] and report["rooms"]
    assert report["previous_range"] and "utilization" in report["previous"]
    assert report["mix"]["ELECTIVE"] >= 9

    case = store.state()["schedule"]["bookings"][0]
    store.record_actual(case["case_id"], case["start"], None)
    with __import__("psycopg").connect(db_url, autocommit=True) as conn:
        conn.execute("UPDATE scheduled_surgeries SET actual_end_time = actual_start_time + INTERVAL '45 minutes' "
                     "WHERE actual_start_time IS NOT NULL AND actual_end_time IS NULL")
    today = store.db.today().isoformat()
    one_day = store.report(today, today)
    assert one_day["cases"] == 10 and one_day["cases_timed"] == 1 and one_day["kpi"]["first_cases_measured"] == 1
    records = store.report_cases(today, today)
    assert len(records) == 10 and all(r.team for r in records)

    from app.store import StoreError

    with pytest.raises(StoreError):
        store.report("2026-12-31", "2026-01-01")
    with pytest.raises(StoreError):
        store.report("2024-01-01", "2026-01-01")  # more than a year


# ------------------------------------------------------------------ phone notifications

def test_notification_store_in_the_database(store, db_url):
    from app.auth import User
    from app.notify import DbNotifyStore, LineSender, Notifier, WebPushSender

    class Accounts:
        class store:  # noqa: N801
            @staticmethod
            def list():
                return [User("u1", "u1", "User One", "staff", "S1")]

    notify_store = DbNotifyStore(store.db)
    n = Notifier(Accounts, notify_store, line=LineSender("", "", ""), background=False)
    user = Accounts.store.list()[0]
    key = n.status(user)["webpush"]["public_key"]
    assert WebPushSender(DbNotifyStore(store.db)).public_key() == key  # one key pair for every server process
    n.subscribe(user, {"endpoint": "https://push.example.org/1", "keys": {"p256dh": "p", "auth": "a"}}, "Phone")
    n.set_prefs(user, {"case": False})
    status = n.status(user)
    assert status["webpush"]["devices"] == ["Phone"] and status["prefs"]["case"] is False
    code = notify_store.new_code("u1")
    assert notify_store.take_code(code) == "u1" and notify_store.take_code(code) is None
    notify_store.log("u1", "webpush", "test", "t", "b", "sent", None)
    assert n.status(user)["recent"][0]["status"] == "sent"
    assert n.unsubscribe(user, "https://push.example.org/1") and n.status(user)["webpush"]["devices"] == []

    with __import__("psycopg").connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE notify_targets")
    assert not notify_store.ready() and n.status(user) == {"ready": False, "kinds": status["kinds"]}
