"""Steps of a case sent by a device in the OR (AI camera, RFID, OR integration) through the HIS API:
POST /api/his/v1/milestones with a key of scope milestones:write.

Runs against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import his as hm
from app.store import StoreError
from test_app import fresh  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, _request, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
ADMIN = {"name": "Admin", "role": "admin"}


@pytest.fixture()
def device(store, monkeypatch):
    his = hm.His(store, lambda event: None)
    monkeypatch.setattr(main, "his", his)
    key = his.create_key("Camera OR", ["milestones:write"], ADMIN)["key"]
    client = TestClient(main.app, base_url="https://testserver")
    client.headers.update({"Authorization": f"Bearer {key}"})
    return his, client


def _book_two(store, day):
    state = store.state(day)
    for hn, at in (("HN-D1", "08:00"), ("HN-D2", "13:00")):
        assert store.slot_check(_request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", hn), at,
                                commit=True, day=day)["booked"], state
    rows = {b["patient_hn"]: b for b in store.state(day)["schedule"]["bookings"] if b["patient_hn"] in ("HN-D1", "HN-D2")}
    return rows["HN-D1"], rows["HN-D2"]


@needs_db
def test_a_camera_that_knows_only_the_room(device, store, db_url):
    his, c = device
    day = _tomorrow(store)
    first, second = _book_two(store, day)
    room = first["or_id"]
    send = lambda **kw: c.post("/api/his/v1/milestones", json={"room": room, "date": day, **kw})  # noqa: E731
    r = send(step="in_room", time="08:05")
    assert r.status_code == 200 and r.json()["status"] == "APPLIED"
    assert (r.json()["case_code"], r.json()["time"]) == (first["case_id"], "08:05")
    assert "HN" not in r.text and "patient" not in r.text  # nothing about the patient comes back
    assert send(step="incision", time="08:30").json()["case_code"] == first["case_id"]  # the case inside
    again = send(step="incision", time="08:33")
    assert again.json()["status"] == "UNCHANGED" and again.json()["time"] == "08:30"  # sent twice: kept
    assert send(step="incision", time="08:33", replace=True).json()["time"] == "08:33"
    assert send(step="out_room", time="09:40").json()["case_code"] == first["case_id"]
    assert send(step="closure", time="09:50").status_code == 400  # nobody in the room now
    assert store.state(day)["milestones"][first["case_id"]]["stage"] == "out_room"
    nxt = c.post("/api/his/v1/milestones", json={"room": second["or_id"], "date": day, "step": "in_room", "time": "13:02"})
    assert nxt.json()["case_code"] == second["case_id"]  # that room's next case not in yet
    assert _rows(db_url, "SELECT DISTINCT recorded_by FROM case_milestones") == [("อุปกรณ์: Camera OR (device)",)]
    assert send(step="arrived").status_code == 400  # holding: the room alone does not say which case
    assert c.post("/api/his/v1/milestones", json={"step": "in_room", "date": day}).status_code == 400  # no case, no room


@needs_db
def test_by_case_code_his_ref_and_iso_time(device, store):
    his, c = device
    day = _tomorrow(store)
    first, _ = _book_two(store, day)
    with pytest.raises(StoreError):
        his.record_step({"step": "in_room", "case_code": "R-NOPE", "date": day}, hm.Client("k", "x", ("milestones:write",)))
    iso = datetime.fromisoformat(f"{day}T08:07:00+07:00").isoformat()
    r = c.post("/api/his/v1/milestones", json={"step": "in_room", "case_code": first["case_id"], "time": iso})
    assert r.status_code == 200 and r.json()["time"] == "08:07" and r.json()["date"] == day
    r = c.post("/api/his/v1/milestones", json={"step": "in_room", "case_code": first["case_id"], "room": "OR-9", "date": day})
    assert r.status_code == 400 and "ห้อง" in r.json()["detail"]
    assert c.post("/api/his/v1/milestones", json={"step": "in_room", "case_code": first["case_id"],
                                                  "time": "2026-11-23T08:07:00"}).status_code == 400  # no zone
    r = c.post("/api/his/v1/milestones", json={"step": "in_room", "his_ref": "OR-NOT-HERE", "date": day})
    assert r.status_code == 404


@needs_db
def test_the_key_needs_the_scope(store, monkeypatch):
    his = hm.His(store, lambda event: None)
    monkeypatch.setattr(main, "his", his)
    key = his.create_key("HOSxP", ["requests:write"], ADMIN)["key"]
    c = TestClient(main.app, base_url="https://testserver")
    r = c.post("/api/his/v1/milestones", json={"step": "in_room", "room": "OR-1"}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 403
    assert c.post("/api/his/v1/milestones", json={"step": "in_room", "room": "OR-1"}).status_code == 401
