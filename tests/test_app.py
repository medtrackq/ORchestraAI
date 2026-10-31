from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import LoginLimiter, MemoryUserStore
from app.main import accounts, app, store
from orchestra_engine import validate_schedule

OWNER = ("orchestra", "owner-secret-123")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", OWNER[1])
    monkeypatch.delenv("APP_USERNAME", raising=False)
    store.reset()
    accounts.store = MemoryUserStore()
    accounts.limiter = LoginLimiter()


def session(username: str, password: str) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    r = c.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture()
def owner() -> TestClient:
    return session(*OWNER)


def make_user(owner: TestClient, username: str, role: str, staff_id: str | None = None) -> TestClient:
    temp = "Temporary-Pass-77"
    r = owner.post("/api/users", json={"username": username, "display_name": username.title(), "role": role,
                                       "staff_id": staff_id, "password": temp})
    assert r.status_code == 200, r.text
    c = session(username, temp)
    assert c.get("/api/state").status_code == 403  # temporary password must be changed first
    assert c.post("/api/password", json={"current_password": temp, "new_password": "Real-Pass-2026!"}).status_code == 200
    return c


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


# ------------------------------------------------------------------ login

def test_everything_but_login_and_health_needs_a_session():
    c = TestClient(app, base_url="https://testserver")
    assert c.get("/api/health").status_code == 200
    assert c.get("/login").status_code == 200
    assert c.get("/api/state").status_code == 401
    page = c.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    assert c.get("/flow", follow_redirects=False).status_code == 303


def test_owner_login_cookie_and_logout(owner):
    me = owner.get("/api/me").json()["user"]
    assert me["role"] == "admin" and "users" in me["permissions"]
    cookie = owner.cookies.get("orchestra_session")
    assert cookie and "." in cookie
    assert owner.post("/api/logout", json={}).status_code == 200
    assert owner.get("/api/state").status_code == 401


def test_wrong_passwords_are_rate_limited():
    c = TestClient(app, base_url="https://testserver")
    for _ in range(5):
        assert c.post("/api/login", json={"username": "orchestra", "password": "nope"}).status_code == 401
    assert c.post("/api/login", json={"username": "orchestra", "password": OWNER[1]}).status_code == 429


def test_tampered_cookie_is_rejected(owner):
    token = owner.cookies.get("orchestra_session")
    body, mac = token.rsplit(".", 1)
    assert accounts.signer.verify(token) is not None
    assert accounts.signer.verify(body + "." + ("0" * len(mac))) is None
    forged = TestClient(app, base_url="https://testserver", cookies={"orchestra_session": body + "." + ("0" * len(mac))})
    assert forged.get("/api/state").status_code == 401
    genuine = TestClient(app, base_url="https://testserver", cookies={"orchestra_session": token})
    assert genuine.get("/api/state").status_code == 200


def test_state_changes_need_json_from_this_site(owner):
    r = owner.post("/api/slot-check", data={"x": "1"})
    assert r.status_code == 415
    r = owner.post("/api/slot-check", json={"case": _case(), "start": "09:00"},
                   headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_passwords_must_be_long_enough(owner):
    r = owner.post("/api/users", json={"username": "short", "display_name": "S", "role": "staff", "password": "abc"})
    assert r.status_code == 400


# ------------------------------------------------------------------ roles

def test_cssd_updates_equipment_but_cannot_book_or_see_patients(owner):
    cssd = make_user(owner, "cssd1", "cssd")
    state = cssd.get("/api/state").json()
    assert {b["patient_hn"] for b in state["schedule"]["bookings"]} == {"•••"}
    assert all(not a["notifications"] for a in state["activity"])
    assert cssd.post("/api/slot-check", json={"case": _case(), "start": "09:00"}).status_code == 403
    assert cssd.get("/api/users").status_code == 403

    # KNEE-01 goes back to CSSD until 13:00 although a TKA uses a knee set at 11:00.
    knee_case = next(b for b in store.result.to_dict()["bookings"] if b["case_id"] == "C02")
    unit = next(e["unit_id"] for e in knee_case["equipment"] if e["unit_id"].startswith("KNEE"))
    r = cssd.patch(f"/api/equipment/{unit}", json={"status": "STERILIZING", "ready_at": "13:00"})
    assert r.status_code == 200
    assert r.json()["result"]["warnings"]  # the scheduler is told which case now conflicts
    assert next(u for u in r.json()["units"] if u["unit_id"] == unit)["ready_at"] == "13:00"
    activity = owner.get("/api/state").json()["activity"][0]
    assert activity["kind"] == "equipment" and "Cssd1" in activity["actor"]
    assert cssd.patch(f"/api/equipment/{unit}", json={"status": "STERILIZING"}).status_code == 400  # needs a time


def test_surgeon_books_only_own_cases(owner):
    surgeon = make_user(owner, "napat", "surgeon", "SUR-GEN")
    assert surgeon.post("/api/slot-check", json={"case": _case(surgeon_id="SUR-ORTHO"), "start": "09:00"}).status_code == 403
    r = surgeon.post("/api/bookings", json={"case": _case(), "start": "09:00"})
    assert r.status_code == 200 and r.json()["result"]["booked"]
    assert "Napat" in r.json()["state"]["activity"][0]["actor"]
    # Overrun of another surgeon's running case is refused.
    r = surgeon.post("/api/overrun", json={"case_id": "C04", "new_duration_mins": 270, "now": "11:00"})
    assert r.status_code == 403
    assert surgeon.post("/api/equipment/KNEE-01", json={}).status_code in (403, 405)


def test_surgeon_account_must_be_linked_to_a_surgeon(owner):
    r = owner.post("/api/users", json={"username": "doc", "display_name": "Doc", "role": "surgeon",
                                       "password": "Temporary-Pass-77"})
    assert r.status_code == 400


def test_staff_sees_only_own_notifications(owner):
    nurse = make_user(owner, "orathai", "staff", "SCR-3")
    assert nurse.post("/api/bookings", json={"case": _case(), "start": "09:00"}).status_code == 403
    owner.post("/api/emergency", json={"case": _case(procedure="Emergency laparotomy", surgeon_id="SUR-ONCALL",
                                                     required_equipment_types=["MAJOR_GENERAL_TRAY"]),
                                       "now": "10:30"})
    everyone = owner.get("/api/state").json()["activity"][0]["notifications"]
    mine = nurse.get("/api/state").json()["activity"][0]["notifications"]
    assert len(mine) < len(everyone)
    assert all(n["staff_id"] == "SCR-3" for n in mine)


def test_disabling_or_resetting_a_user_ends_their_session(owner):
    sched = make_user(owner, "sched1", "scheduler")
    user_id = sched.get("/api/me").json()["user"]["user_id"]
    assert sched.get("/api/state").status_code == 200
    assert owner.patch(f"/api/users/{user_id}", json={"is_active": False}).status_code == 200
    assert sched.get("/api/state").status_code == 401
    assert owner.patch(f"/api/users/{user_id}", json={"is_active": True}).status_code == 200
    assert owner.post(f"/api/users/{user_id}/reset-password", json={"password": "Another-temp-1"}).status_code == 200
    again = session("sched1", "Another-temp-1")
    assert again.get("/api/me").json()["user"]["must_change_password"] is True


# --------------------------------------------------------------- schedule

def test_slot_check_previews_without_booking(owner):
    before = len(store.result.assignments)
    r = owner.post("/api/slot-check", json={"case": _case(), "start": "09:00"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["verdicts"]) == 3 and body["booked"] is False
    assert len(store.result.assignments) == before


def test_booking_commits_a_conflict_free_schedule(owner):
    before = len(store.result.assignments)
    r = owner.post("/api/bookings", json={"case": _case(), "start": "09:00"})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["booked"] and result["case_id"] == "R001"
    assert len(store.result.assignments) == before + 1
    assert _stored_schedule_is_valid()


def test_emergency_and_overrun_ripple(owner):
    er = _case(procedure="Emergency laparotomy", surgeon_id="SUR-ONCALL",
               required_equipment_types=["MAJOR_GENERAL_TRAY"], requires_icu_post_op=True, infection_risk="DIRTY")
    r = owner.post("/api/emergency", json={"case": er, "now": "10:30"})
    assert r.status_code == 200 and "E001" in store.result.assignments
    assert _stored_schedule_is_valid()
    started = next(a for a in store.result.assignments.values() if a.start_min < 11 * 60 < a.end_min)
    r = owner.post("/api/overrun", json={"case_id": started.case_id,
                                         "new_duration_mins": started.end_min - started.start_min + 45, "now": "11:00"})
    assert r.status_code == 200
    assert store.result.assignments[started.case_id].end_min == started.end_min + 45
    assert _stored_schedule_is_valid()


def test_bad_requests_are_rejected_with_a_message(owner):
    r = owner.post("/api/slot-check", json={"case": _case(surgeon_id="NOPE"), "start": "09:00"})
    assert r.status_code == 400 and "NOPE" in r.json()["detail"]
    assert owner.post("/api/slot-check", json={"case": _case(), "start": "9am"}).status_code == 422
    r = owner.post("/api/overrun", json={"case_id": "C05", "new_duration_mins": 200, "now": "09:00"})
    assert r.status_code == 400


def test_reset_restores_demo_day(owner):
    owner.post("/api/bookings", json={"case": _case(), "start": "09:00"})
    state = owner.post("/api/reset", json={}).json()["state"]
    assert len(state["schedule"]["bookings"]) == 10


def test_pages_are_served(owner):
    assert "ORchestraAI" in owner.get("/").text
    assert 'id="flow-data"' in owner.get("/flow").text
