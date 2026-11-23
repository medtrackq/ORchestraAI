"""A patient's RFID wristband read at an OR door: POST /api/his/v1/wristband (key scope milestones:write).

The reader sends what the band carries (the HN) and the room whose door it is at; the step (into or out
of the OR) is worked out, the many reads of one pass count once, and a band read at another room's
door records nothing and tells the schedulers. Runs against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import his as hm
from app import notify
from test_app import fresh  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, _request, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
ADMIN = {"name": "Admin", "role": "admin"}
HN = "HN-65/0123"


@pytest.fixture()
def reader(store, monkeypatch):
    events = []
    his = hm.His(store, events.append)
    monkeypatch.setattr(main, "his", his)
    key = his.create_key("RFID ประตู OR", ["milestones:write"], ADMIN)["key"]
    client = TestClient(main.app, base_url="https://testserver")
    client.headers.update({"Authorization": f"Bearer {key}"})
    return his, client, events


def _book(store, day, hn=HN, at="08:00"):
    assert store.slot_check(_request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", hn), at,
                            commit=True, day=day)["booked"]
    return next(b for b in store.state(day)["schedule"]["bookings"] if b["patient_hn"] == hn)


@needs_db
def test_a_door_reader_works_out_in_and_out(reader, store, db_url):
    his, c, events = reader
    day = _tomorrow(store)
    case = _book(store, day)
    room = case["or_id"]
    read = lambda at, **kw: c.post("/api/his/v1/wristband", json={"tag": "hn 65-0123", "room": room,  # noqa: E731
                                                                   "date": day, "time": at, **kw})
    r = read("08:04")
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["step"], r.json()["case_code"], r.json()["time"]) == \
        ("APPLIED", "in_room", case["case_id"], "08:04")
    assert "0123" not in r.text and "patient" not in r.text  # the HN never comes back
    assert read("08:04").json()["status"] == "DUPLICATE"  # the reader reads the band many times as it passes
    assert read("08:09").json()["status"] == "DUPLICATE"
    assert store.state(day)["milestones"][case["case_id"]]["stage"] == "in_room"
    r = read("09:35")
    assert (r.json()["status"], r.json()["step"]) == ("APPLIED", "out_room")
    assert read("09:36").json()["status"] == "DUPLICATE"
    assert read("11:00").json()["status"] == "IGNORED"  # nothing left for this patient today
    assert store.state(day)["milestones"][case["case_id"]]["times"]["out_room"] == "09:35"
    assert _rows(db_url, "SELECT s.actual_start_time IS NOT NULL, s.actual_end_time IS NOT NULL FROM scheduled_surgeries s "
                         "JOIN surgery_requests r ON r.request_id = s.request_id WHERE r.patient_hn = %s", HN) == [(True, True)]
    assert events == []


@needs_db
def test_the_wrong_rooms_door_records_nothing_and_tells_the_schedulers(reader, store):
    his, c, events = reader
    day = _tomorrow(store)
    case = _book(store, day)
    other = next(r["id"] for r in store.state(day)["options"]["rooms"] if r["id"] != case["or_id"])
    read = lambda room, at: c.post("/api/his/v1/wristband", json={"tag": HN, "room": room, "date": day, "time": at})  # noqa: E731
    r = read(other, "08:02")
    assert r.status_code == 200
    body = r.json()
    assert (body["status"], body["room"], body["reader_room"]) == ("WRONG_ROOM", case["or_id"], other)
    assert store.state(day)["milestones"][case["case_id"]]["times"]["in_room"] is None
    assert read(other, "08:02").json()["status"] == "WRONG_ROOM"
    assert len(events) == 1  # once per case and door, however many times the band is read
    event = events[0]
    assert event["what"] == "wrong_room" and case["case_id"] in event["detail"] and "0123" not in str(event)
    messages = notify.build_messages(event, [_user("scheduler"), _user("surgeon")])
    assert len(messages) == 1 and "ผิดห้อง" in messages[0].title and "0123" not in messages[0].body
    assert read(case["or_id"], "08:06").json()["status"] == "APPLIED"  # then the right door


def _user(role):
    from types import SimpleNamespace

    return SimpleNamespace(user_id="u1", role=role, staff_id=None)


@needs_db
def test_holding_and_recovery_readers_and_a_dry_run(reader, store):
    his, c, _ = reader
    day = _tomorrow(store)
    case = _book(store, day)
    send = lambda **kw: c.post("/api/his/v1/wristband", json={"tag": HN, "date": day, **kw})  # noqa: E731
    r = send(step="arrived", time="07:40")
    assert (r.json()["status"], r.json()["case_code"]) == ("APPLIED", case["case_id"])
    assert send(step="arrived", time="07:41").json()["status"] == "UNCHANGED"
    assert send(step="recovery_out", time="07:42").status_code == 400  # not out of the OR yet
    trial = send(room=case["or_id"], time="08:00", dry_run=True)
    assert (trial.json()["status"], trial.json()["step"]) == ("DRY_RUN", "in_room")
    assert store.state(day)["milestones"][case["case_id"]]["times"]["in_room"] is None  # nothing recorded
    assert send(room=case["or_id"], step="in_room", time="08:00").json()["status"] == "APPLIED"
    assert send(room=case["or_id"], step="out_room", time="09:30").json()["status"] == "APPLIED"
    assert send(step="recovery_out", time="10:30").json()["status"] == "APPLIED"
    assert send(step="recovery_out", time="10:31").json()["status"] == "UNCHANGED"
    assert store.state(day)["milestones"][case["case_id"]]["stage"] == "recovery_out"


@needs_db
def test_unknown_band_missing_fields_and_the_scope(reader, store, monkeypatch):
    his, c, _ = reader
    day = _tomorrow(store)
    _book(store, day)
    r = c.post("/api/his/v1/wristband", json={"tag": "HN-9999999", "room": "OR-1", "date": day, "time": "08:00"})
    assert r.status_code == 404 and "9999999" not in r.text
    assert c.post("/api/his/v1/wristband", json={"tag": HN, "date": day}).status_code == 400  # neither room nor step
    assert c.post("/api/his/v1/wristband", json={"room": "OR-1"}).status_code == 422
    assert c.post("/api/his/v1/wristband", json={"tag": "HN\n1", "room": "OR-1"}).status_code == 422
    r = c.post("/api/his/v1/wristband", json={"tag": HN, "room": "OR-1", "date": day})
    assert r.status_code == 400  # another day: the reader must send the time
    other = hm.His(store, lambda event: None)
    monkeypatch.setattr(main, "his", other)
    key = other.create_key("HOSxP", ["schedule:read"], ADMIN)["key"]
    plain = TestClient(main.app, base_url="https://testserver")
    assert plain.post("/api/his/v1/wristband", json={"tag": HN, "room": "OR-1"},
                      headers={"Authorization": f"Bearer {key}"}).status_code == 403
    assert plain.post("/api/his/v1/wristband", json={"tag": HN, "room": "OR-1"}).status_code == 401


def test_band_values_are_compared_without_spaces_dashes_or_slashes():
    assert hm._band("hn 65/0123") == hm._band("HN-650123") == "HN650123"
    assert hm._band(None) == ""
