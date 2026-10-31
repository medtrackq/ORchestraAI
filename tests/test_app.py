from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.main import app, store
from orchestra_engine import validate_schedule

AUTH = {"Authorization": "Basic " + base64.b64encode(b"orchestra:secret").decode()}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "secret")
    monkeypatch.delenv("APP_USERNAME", raising=False)
    store.reset()
    with TestClient(app) as c:
        yield c


def _stored_schedule_is_valid() -> bool:
    h, r = store.hospital, store.result
    return validate_schedule(r, r.cases.values(), h.rooms, h.equipments, h.staff, h.beds) == []


def _case(**overrides):
    case = {
        "procedure": "Laparoscopic cholecystectomy", "patient_hn": "HN-1", "surgeon_id": "SUR-GEN",
        "duration_mins": 60, "required_equipment_types": ["LAPAROSCOPY_TRAY"],
    }
    case.update(overrides)
    return case


def test_login_is_required_except_health(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/state").status_code == 401
    assert client.get("/").status_code == 401
    bad = {"Authorization": "Basic " + base64.b64encode(b"orchestra:wrong").decode()}
    assert client.get("/api/state", headers=bad).status_code == 401
    assert client.get("/api/state", headers=AUTH).status_code == 200


def test_pages_are_served(client):
    assert "ORchestraAI" in client.get("/", headers=AUTH).text
    assert 'id="flow-data"' in client.get("/flow", headers=AUTH).text


def test_slot_check_previews_without_booking(client):
    before = len(store.result.assignments)
    r = client.post("/api/slot-check", json={"case": _case(), "start": "09:00"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert len(body["verdicts"]) == 3 and body["booked"] is False
    assert len(store.result.assignments) == before


def test_booking_commits_a_conflict_free_schedule(client):
    before = len(store.result.assignments)
    r = client.post("/api/bookings", json={"case": _case(), "start": "09:00"}, headers=AUTH)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["booked"] and result["case_id"] == "R001"
    assert len(store.result.assignments) == before + 1
    assert _stored_schedule_is_valid()
    second = client.post("/api/bookings", json={"case": _case(patient_hn="HN-2"), "start": "09:00"}, headers=AUTH)
    assert second.json()["result"]["case_id"] == "R002"
    assert _stored_schedule_is_valid()


def test_emergency_and_overrun_ripple(client):
    er = _case(procedure="Emergency laparotomy", surgeon_id="SUR-ONCALL",
               required_equipment_types=["MAJOR_GENERAL_TRAY"], requires_icu_post_op=True, infection_risk="DIRTY")
    r = client.post("/api/emergency", json={"case": er, "now": "10:30"}, headers=AUTH)
    assert r.status_code == 200
    assert "E001" in store.result.assignments
    assert any(c["case_id"] == "E001" for c in r.json()["result"]["changes"])
    assert _stored_schedule_is_valid()

    started = next(a for a in store.result.assignments.values() if a.start_min < 11 * 60 < a.end_min)
    new_duration = started.end_min - started.start_min + 45
    r = client.post("/api/overrun", json={"case_id": started.case_id, "new_duration_mins": new_duration,
                                          "now": "11:00"}, headers=AUTH)
    assert r.status_code == 200
    assert store.result.assignments[started.case_id].end_min == started.end_min + 45
    assert _stored_schedule_is_valid()
    kinds = [a["kind"] for a in client.get("/api/state", headers=AUTH).json()["activity"]]
    assert kinds[:3] == ["overrun", "emergency", "reset"]


def test_bad_requests_are_rejected_with_a_message(client):
    r = client.post("/api/slot-check", json={"case": _case(surgeon_id="NOPE"), "start": "09:00"}, headers=AUTH)
    assert r.status_code == 400 and "NOPE" in r.json()["detail"]
    r = client.post("/api/slot-check", json={"case": _case(required_equipment_types=["X"]), "start": "09:00"},
                    headers=AUTH)
    assert r.status_code == 400
    r = client.post("/api/slot-check", json={"case": _case(), "start": "9am"}, headers=AUTH)
    assert r.status_code == 422
    r = client.post("/api/overrun", json={"case_id": "C05", "new_duration_mins": 200, "now": "09:00"}, headers=AUTH)
    assert r.status_code == 400  # C05 has not started at 09:00


def test_reset_restores_demo_day(client):
    client.post("/api/bookings", json={"case": _case(), "start": "09:00"}, headers=AUTH)
    state = client.post("/api/reset", headers=AUTH).json()["state"]
    assert len(state["schedule"]["bookings"]) == 10
