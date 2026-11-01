"""The first free days across several days, and the waiting list (due dates, order, warnings).

The database tests run against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

from datetime import date, timedelta
from uuid import uuid4

import psycopg
import pytest

from app import waitlist as wl
from app.store import Forbidden, StoreError
from test_app import OWNER, _case, fresh, make_user, owner, session  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, _request, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
ADMIN = {"name": "Admin A", "username": "a", "role": "admin", "staff_id": None, "user_id": "u-admin"}


# ------------------------------------------------------------------ rules

def test_due_dates_and_order():
    today = date(2026, 11, 2)
    assert wl.due_info(None, today) == {"due_date": None, "days_left": None, "due_status": None}
    assert wl.due_info(date(2026, 11, 1), today)["due_status"] == "overdue"
    assert wl.due_info(date(2026, 11, 9), today) == {"due_date": "2026-11-09", "days_left": 7, "due_status": "soon"}
    assert wl.due_info(date(2026, 11, 10), today)["due_status"] == "ok"
    assert wl.parse_due("", today) is None and wl.parse_due("2026-12-01", today) == date(2026, 12, 1)
    for bad in ("1/12/2569", "2026-13-01"):
        with pytest.raises(StoreError, match="วันที่ควรผ่าตัดไม่ถูกต้อง"):
            wl.parse_due(bad, today)
    with pytest.raises(StoreError, match="2 ปี"):
        wl.parse_due("2030-01-01", today)

    rows = [
        {"code": "a", "urgency": "ELECTIVE", "due_date": None, "waiting_days": 30},
        {"code": "b", "urgency": "ELECTIVE", "due_date": "2026-11-20", "waiting_days": 1},
        {"code": "c", "urgency": "ELECTIVE", "due_date": "2026-11-05", "waiting_days": 1},
        {"code": "d", "urgency": "URGENT", "due_date": None, "waiting_days": 0},
        {"code": "e", "urgency": "ELECTIVE", "due_date": None, "waiting_days": 40},
    ]
    assert [r["code"] for r in sorted(rows, key=wl.order_key)] == ["d", "c", "b", "e", "a"]


def test_search_days_and_start():
    monday = date(2026, 11, 2)
    assert len(wl.search_days(monday, 14, True)) == 10 and len(wl.search_days(monday, 14, False)) == 14
    assert len(wl.search_days(monday, 99, False)) == wl.MAX_SEARCH_DAYS
    assert wl.search_start(9 * 60, 0) == 540
    assert wl.search_start(9 * 60, 10 * 60 + 2) == 10 * 60 + 5  # later today: from now, rounded up
    assert wl.search_start(9 * 60, 24 * 60) is None
    assert wl.thai_date(monday) == "2 พ.ย. 2569"


# ------------------------------------------------------------------ search (Supabase)

@pytest.fixture()
def admin(store):
    from app.admin_store import Inventory

    return Inventory(store)


@needs_db
def test_search_offers_the_first_free_days_and_books_nothing(store, admin, db_url):
    day = _tomorrow(store)
    data = _request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-S1")
    booked_before = _rows(db_url, "SELECT count(*) FROM scheduled_surgeries")
    out = store.find_slots(data, "09:00", day=day, skip_weekends=False)
    assert [f["date"] for f in out["found"]] == [
        (date.fromisoformat(day) + timedelta(days=i)).isoformat() for i in range(3)]
    first = out["found"][0]
    assert (out["from"], out["to"], out["searched"]) == (day, out["found"][-1]["date"], 3)
    assert out["next_from"] == (date.fromisoformat(day) + timedelta(days=3)).isoformat()  # carry on after the last day checked
    assert first["as_requested"] and first["start"] == "09:00" and first["team"][0]["name"].startswith("Dr.Napat")
    assert _rows(db_url, "SELECT count(*) FROM scheduled_surgeries") == booked_before  # nothing booked

    for room in {r["id"] for r in store.state(day)["options"]["rooms"]}:  # every room closed tomorrow
        admin.add_closure({"room": room, "start_date": day, "start_time": "00:00", "end_date": day, "end_time": "23:59",
                           "reason": "ซ่อมระบบไฟ"})
    later = store.find_slots({**data, "due_date": day}, "09:00", day=day, skip_weekends=False)
    assert later["found"][0]["date"] > day and later["found"][0]["after_due"]
    assert later["skipped"][0]["date"] == day and later["skipped"][0]["reason"]
    weekdays = store.find_slots(data, "09:00", day=day, skip_weekends=True, days=14)
    assert all(date.fromisoformat(f["date"]).weekday() < 5 for f in weekdays["found"])

    pick = later["found"][0]
    result = store.slot_check(data, pick["start"], commit=True, day=pick["date"])
    assert result["booked"] and result["requested_start"] == pick["start"]

    with pytest.raises(StoreError, match="ฉุกเฉิน"):
        store.find_slots({**data, "urgency": "EMERGENCY"}, "09:00")


@needs_db
def test_search_stops_in_time_and_says_where_to_carry_on(store, monkeypatch):
    monkeypatch.setattr(wl, "SEARCH_SECONDS", 0.0)
    day = _tomorrow(store)
    data = _request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-S2")
    out = store.find_slots(data, "09:00", day=day, skip_weekends=False)
    assert out["searched"] == 1 and out["carry_on"] == (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    assert out["next_from"] == out["carry_on"]


# ------------------------------------------------------------------ waiting list (Supabase)

@needs_db
def test_waiting_list_due_dates_order_and_rebooking(store, db_url):
    today = store.db.today()
    state = store.state()
    base = _request(state, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-Q0")
    add = lambda hn, **extra: store.add_waiting({**base, "patient_hn": hn, "patient_name": f"Test {hn}", **extra},  # noqa: E731
                                               actor=ADMIN)
    plain = add("HN-Q1")
    soon = add("HN-Q2", due_date=(today + timedelta(days=3)).isoformat())
    late = add("HN-Q3", due_date=(today - timedelta(days=2)).isoformat())
    urgent = add("HN-Q4", urgency="URGENT")
    pending = store.state()["pending"]
    assert [p["code"] for p in pending] == [urgent["code"], late["code"], soon["code"], plain["code"]]
    by_code = {p["code"]: p for p in pending}
    assert (by_code[late["code"]]["due_status"], by_code[late["code"]]["days_left"]) == ("overdue", -2)
    assert by_code[soon["code"]]["due_status"] == "soon" and by_code[plain["code"]]["waiting_days"] == 0
    assert by_code[plain["code"]]["was_on"] is None and store.state()["waitlist_ready"]
    assert "เพิ่มเข้ารายการรอผ่าตัด" in _rows(db_url, "SELECT title FROM orchestra_activity WHERE kind = 'waitlist' LIMIT 1")[0][0]

    with pytest.raises(StoreError, match="อยู่แล้ว"):
        add("HN-Q1")  # the same patient and procedure twice
    with pytest.raises(StoreError, match="ฉุกเฉิน"):
        add("HN-Q5", urgency="EMERGENCY")
    with pytest.raises(StoreError, match="ชื่อผู้ป่วย"):
        store.add_waiting({**base, "patient_hn": "HN-Q6", "patient_name": ""})

    assert store.set_due(plain["id"], (today + timedelta(days=30)).isoformat(), actor=ADMIN)["due_date"]
    assert store.set_due(late["id"], None)["due_date"] is None
    other = {"name": "Dr.X", "username": "x", "role": "surgeon", "staff_id": str(uuid4())}
    with pytest.raises(Forbidden):
        store.set_due(soon["id"], None, actor=other)
    with pytest.raises(StoreError, match="ถูกจองหรือยกเลิก"):
        store.set_due(str(uuid4()), None)

    # rebooking keeps the request; the due date goes with it, and a postponed case keeps it on the list
    day = _tomorrow(store)
    due = (today + timedelta(days=3)).isoformat()
    rebook = {**base, "patient_hn": "HN-Q2", "patient_name": "Test HN-Q2", "pending_id": soon["id"], "due_date": due}
    assert store.slot_check(rebook, "09:00", commit=True, day=day)["booked"]
    assert _rows(db_url, "SELECT status, due_date::text FROM surgery_requests WHERE patient_hn = 'HN-Q2'") == [("SCHEDULED", due)]
    case = next(b["case_id"] for b in store.state(day)["schedule"]["bookings"] if b["patient_hn"] == "HN-Q2")
    store.cancel_booking(case, {"reason": "NPO", "outcome": "POSTPONED"}, day=day, actor=ADMIN)
    back = next(p for p in store.state()["pending"] if p["patient_hn"] == "HN-Q2")
    assert back["due_date"] == due and back["was_on"] == day and back["waiting_days"] == 0


@needs_db
def test_works_before_migration_015(store, db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE surgery_requests DROP COLUMN due_date, DROP COLUMN waitlisted_at")
    state = store.state()
    assert state["waitlist_ready"] is False
    data = _request(state, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-R1")
    added = store.add_waiting(data)
    [row] = store.state()["pending"]
    assert row["code"] == added["code"] and row["due_date"] is None and row["waiting_days"] == 0
    due = (store.db.today() + timedelta(days=9)).isoformat()
    for call in (lambda: store.add_waiting({**data, "patient_hn": "HN-R2", "due_date": due}),
                 lambda: store.set_due(added["id"], due),
                 lambda: store.slot_check({**data, "patient_hn": "HN-R3", "due_date": due}, "09:00", commit=True,
                                          day=_tomorrow(store))):
        with pytest.raises(StoreError, match="015"):
            call()
    assert store.slot_check({**data, "patient_hn": "HN-R3"}, "09:00", commit=True, day=_tomorrow(store))["booked"]


# ------------------------------------------------------------------ API (demo store)

def test_api_search_and_waiting_list(owner):
    r = owner.post("/api/slot-search", json={"case": _case(), "start": "09:00"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["single_day"] and len(out["found"]) + len(out["skipped"]) == 1

    due = (date.today() + timedelta(days=20)).isoformat()
    r = owner.post("/api/waitlist", json={"case": _case(patient_hn="HN-W9", due_date=due)})
    assert r.status_code == 200, r.text
    code = r.json()["result"]["code"]
    row = next(p for p in r.json()["state"]["pending"] if p["code"] == code)
    assert (row["due_date"], row["due_status"], row["was_on"]) == (due, "ok", None)
    assert r.json()["state"]["activity"][0]["kind"] == "waitlist"
    assert owner.post("/api/waitlist", json={"case": _case(patient_hn="HN-W9")}).status_code == 400  # already waiting
    assert owner.post("/api/waitlist", json={"case": _case(due_date="1/1/2570")}).status_code == 422

    r = owner.patch(f"/api/pending/{code}", json={"due_date": None})
    assert r.status_code == 200 and next(p for p in r.json()["state"]["pending"] if p["code"] == code)["due_date"] is None

    cssd = make_user(owner, "cssd7", "cssd")
    assert cssd.post("/api/slot-search", json={"case": _case(), "start": "09:00"}).status_code == 403
    assert cssd.post("/api/waitlist", json={"case": _case(patient_hn="HN-W8")}).status_code == 403
    assert cssd.patch(f"/api/pending/{code}", json={"due_date": due}).status_code == 403
    surgeon = make_user(owner, "napat7", "surgeon", "SUR-GEN")
    assert surgeon.post("/api/slot-search", json={"case": _case(surgeon_id="SUR-ORTHO"), "start": "09:00"}).status_code == 403
    assert surgeon.post("/api/waitlist", json={"case": _case(surgeon_id="SUR-ORTHO", patient_hn="HN-W7")}).status_code == 403
    assert surgeon.patch(f"/api/pending/{code}", json={"due_date": due}).status_code == 200  # own (SUR-GEN) request
