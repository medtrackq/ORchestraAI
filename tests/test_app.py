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


# ------------------------------------------------------------- staffing agent

def test_staff_fatigue_view_is_shaped_per_role(owner):
    state = owner.get("/api/state").json()
    assert len(state["staff"]) == len(store.hospital.staff)
    person = next(p for p in state["staff"] if p["staff_id"] == "ANE-2")
    assert person["week_mins"] >= 45 * 60 and person["daily_cap_mins"] == 600 and 0 <= person["score"] <= 100
    assert state["options"]["skills"]["SCRUB_NURSE"] == ["ROBOTIC_CERTIFIED"]

    nurse = make_user(owner, "malee", "staff", "SCR-1")
    mine = nurse.get("/api/state").json()
    assert [p["staff_id"] for p in mine["staff"]] == ["SCR-1"]  # only their own hours
    assert "staff_view" in mine["me"]["permissions"] and "staff_all" not in mine["me"]["permissions"]
    cssd = make_user(owner, "cssd1", "cssd")
    assert cssd.get("/api/state").json()["staff"] == []


def test_required_skills_reach_the_engine(owner):
    case = _case(surgeon_id="SUR-ONCALL", required_staff_skills={"ANESTHESIOLOGIST": ["PEDIATRIC_ANESTHESIA"]})
    r = owner.post("/api/slot-check", json={"case": case, "start": "16:00"}).json()
    placed = next(b for b in r["result"]["bookings"] if b["case_id"] == r["case_id"])
    assert "ANE-3" in [t["staff_id"] for t in placed["team"]]  # the only paediatric anaesthetist

    body = {"case": _case(required_staff_skills={"SCRUB_NURSE": ["CARDIAC"]}), "start": "10:00"}
    r = owner.post("/api/slot-check", json=body).json()
    staffing = next(v for v in r["verdicts"] if v["agent"] == "Staffing Agent")
    assert any(i["note"] == "ไม่มีใครมีทักษะนี้" for i in staffing["items"])
    assert not any(b["case_id"] == r["case_id"] for b in r["result"]["bookings"])

    bad = {"case": _case(required_staff_skills={"SCRUB_NURSE": ["drop table"]}), "start": "10:00"}
    assert owner.post("/api/slot-check", json=bad).status_code == 400
    bad = {"case": _case(required_staff_skills={"JANITOR": ["MOP"]}), "start": "10:00"}
    assert owner.post("/api/slot-check", json=bad).status_code == 400


def test_displaced_case_waits_and_can_be_cancelled(owner):
    c04 = store.result.assignments["C04"]
    now = f"{(c04.start_min + 30) // 60:02d}:{(c04.start_min + 30) % 60:02d}"
    r = owner.post("/api/overrun", json={"case_id": "C04", "new_duration_mins": 600, "now": now}).json()
    waiting = r["state"]["pending"]
    assert [p["code"] for p in waiting] == ["C05"]  # the next robotic case no longer fits today
    assert waiting[0]["patient_hn"] == "HN-0005"

    cssd = make_user(owner, "cssd2", "cssd")
    assert cssd.get("/api/state").json()["pending"][0]["patient_hn"] == "•••"
    assert cssd.post("/api/pending/C05/cancel", json={}).status_code == 403

    r = owner.post("/api/pending/C05/cancel", json={})
    assert r.status_code == 200 and r.json()["state"]["pending"] == []
    assert owner.post("/api/pending/C05/cancel", json={}).status_code == 400
    assert owner.post("/api/replan", json={}).status_code == 200


def test_supplies_are_checked_when_booking(owner):
    state = owner.get("/api/state").json()
    drape = next(s for s in state["options"]["supplies"] if s["id"] == "ROBO-DRAPE")
    assert (drape["available"], drape["booked"], drape["left"], drape["low"]) == (2, 2, 0, True)
    knee = next(p for p in state["options"]["procedures"] if p["name"] == "Total knee arthroplasty (L)")
    assert knee["supplies"] == {"KNEE-IMP": 1, "BONE-CEM": 2}

    body = {"case": _case(surgeon_id="SUR-ONCALL", required_supplies={"ROBO-DRAPE": 1}), "start": "17:00"}
    r = owner.post("/api/slot-check", json=body).json()
    equipment = next(v for v in r["verdicts"] if v["agent"] == "Equipment & CSSD Agent")
    assert not equipment["ok"] and "ต้องใช้ 1 แต่เหลือเพียง 0" in equipment["items"][-1]["note"]
    assert "Robotic drape kit" in r["result"]["unscheduled"][-1]["reasons"][0]

    body = {"case": _case(surgeon_id="SUR-ONCALL", required_supplies={"MESH-15": 1}), "start": "17:00"}
    r = owner.post("/api/bookings", json=body).json()
    assert r["result"]["booked"]
    booked = next(b for b in r["state"]["schedule"]["bookings"] if b["case_id"] == r["result"]["case_id"])
    assert booked["supplies"] == {"MESH-15": 1}
    assert next(s for s in r["state"]["options"]["supplies"] if s["id"] == "MESH-15")["left"] == 2

    bad = {"case": _case(required_supplies={"GOLD-BAR": 1}), "start": "10:00"}
    assert owner.post("/api/slot-check", json=bad).status_code == 400


def test_management_report_and_csv(owner):
    r = owner.get("/api/reports").json()
    assert r["cases"] == 10 and r["mix"]["ELECTIVE"] == 9 and r["kpi"]["utilization"] > 0
    assert {"rooms", "per_day", "error_bins", "staff_hours", "waiting"} <= set(r)
    csv_text = owner.get("/api/reports/cases.csv").text
    lines = csv_text.strip().splitlines()
    assert lines[0].lstrip("﻿").startswith("case,date,room") and len(lines) == 11
    assert "HN-" not in csv_text  # no patient identifiers in the export
    assert owner.get("/reports").status_code == 200

    scheduler = make_user(owner, "sched1", "scheduler")
    assert scheduler.get("/api/reports").status_code == 200
    nurse = make_user(owner, "nurse9", "staff", "SCR-2")
    assert nurse.get("/api/reports").status_code == 403
    assert nurse.get("/api/reports/cases.csv").status_code == 403


def test_preference_cards_need_supabase_and_the_cards_permission(owner):
    assert owner.get("/api/admin/cards").status_code == 400  # demo data cannot be edited
    surgeon = make_user(owner, "napat2", "surgeon", "SUR-GEN")
    assert "cards" in surgeon.get("/api/me").json()["user"]["permissions"]
    assert surgeon.get("/api/admin/cards").status_code == 400  # allowed, but needs Supabase
    nurse = make_user(owner, "nurse8", "staff", "SCR-2")
    assert nurse.get("/api/admin/cards").status_code == 403
    cssd = make_user(owner, "cssd8", "cssd")
    assert cssd.post("/api/admin/cards", json={"duration": 60}).status_code == 403
    assert cssd.request("DELETE", "/api/admin/cards/x", json={}).status_code == 403
    assert owner.post("/api/admin/cards", json={"duration": 5}).status_code == 422  # out of range


def test_excel_import_and_clean_up_are_for_the_admin_in_supabase_mode(owner):
    import base64

    body = {"filename": "x.csv", "content": base64.b64encode(b"a,b\n").decode()}
    assert owner.get("/api/admin/export.xlsx").status_code == 400  # demo data cannot be exported or replaced
    assert owner.post("/api/admin/import", json=body).status_code == 400
    assert owner.get("/api/admin/demo-data").status_code == 400
    scheduler = make_user(owner, "sched7", "scheduler")
    assert scheduler.get("/api/admin/export.xlsx").status_code == 403
    assert scheduler.post("/api/admin/import", json=body).status_code == 403
    assert scheduler.post("/api/admin/wipe", json={"confirm": "ล้างข้อมูล"}).status_code == 403
    assert scheduler.post("/api/admin/demo-data/delete-cases", json={}).status_code == 403
    assert owner.post("/api/admin/import", json={**body, "content": ""}).status_code == 422


def test_roster_excel_is_for_schedulers_and_admins(owner):
    import base64

    body = {"filename": "r.csv", "content": base64.b64encode("ชื่อ,1\nนก,ช\n".encode()).decode(), "month": "2026-11"}
    scheduler = make_user(owner, "sched8", "scheduler")
    assert scheduler.get("/api/admin/roster.xlsx").status_code == 400  # allowed, but needs Supabase
    assert scheduler.post("/api/admin/roster/import", json=body).status_code == 400
    nurse = make_user(owner, "nurse7", "staff", "SCR-2")
    assert nurse.get("/api/admin/roster.xlsx").status_code == 403
    assert nurse.post("/api/admin/roster/import", json=body).status_code == 403
    cssd = make_user(owner, "cssd7", "cssd")
    assert cssd.post("/api/admin/roster/import", json=body).status_code == 403


def test_readiness_checklist_over_the_api(owner):
    state = owner.get("/api/state").json()
    knee = next(b for b in state["schedule"]["bookings"] if b["procedure"].startswith("Total knee"))
    url = f"/api/cases/{knee['case_id']}/readiness"
    nurse = make_user(owner, "nurse6", "staff", "SCR-2")
    r = nurse.post(url, json={"item": "npo", "status": "PROBLEM", "note": "ดื่มน้ำ 07:00"})
    assert r.status_code == 200 and r.json()["state"]["readiness"][knee["case_id"]]["status"] == "problem"
    assert nurse.post(url, json={"item": "npo", "status": "PROBLEM"}).status_code == 400  # say what the problem is
    assert nurse.post(url, json={"item": "npo", "status": "MAYBE"}).status_code == 422
    other = make_user(owner, "kittipong", "surgeon", "SUR-URO")
    assert other.post(url, json={"item": "consent", "status": "DONE"}).status_code == 403  # not their case
    own = make_user(owner, "somchai", "surgeon", "SUR-ORTHO")
    assert own.post(url, json={"item": "consent", "status": "DONE"}).status_code == 200
    cssd = make_user(owner, "cssd6", "cssd")
    assert cssd.post(url, json={"item": "consent", "status": "DONE"}).status_code == 403
    seen = cssd.get("/api/state").json()["readiness"][knee["case_id"]]
    assert seen["status"] == "problem" and seen["items"]["npo"]["note"] is None  # status only, no clinical note
    assert owner.get("/api/state").json()["readiness"][knee["case_id"]]["items"]["npo"]["note"] == "ดื่มน้ำ 07:00"


def test_print_page_and_the_data_it_uses(owner, monkeypatch):
    assert TestClient(app, base_url="https://testserver").get("/print", follow_redirects=False).status_code in (302, 303)
    assert owner.get("/print").status_code == 200
    monkeypatch.setenv("HOSPITAL_NAME", "โรงพยาบาลทดสอบ")
    state = owner.get("/api/state").json()
    assert state["hospital_name"] == "โรงพยาบาลทดสอบ"
    team = state["schedule"]["bookings"][0]["team"]
    assert team[0]["role"] == "SURGEON" and {t["role"] for t in team[1:]} <= {
        "SURGEON", "ANESTHESIOLOGIST", "SCRUB_NURSE", "CIRCULATING_NURSE"}
    monkeypatch.delenv("HOSPITAL_NAME")
    assert owner.get("/api/state").json()["hospital_name"] is None


def test_calling_off_a_case_over_the_api(owner):
    state = owner.get("/api/state").json()
    assert {"key": "NPO", "label": "ผู้ป่วยไม่ได้งดน้ำงดอาหาร", "group": "patient"} in state["cancel_reasons"]
    knee = next(b for b in state["schedule"]["bookings"] if b["procedure"].startswith("Total knee"))
    url = f"/api/cases/{knee['case_id']}/cancel"
    nurse = make_user(owner, "nurse5", "staff", "SCR-2")
    assert nurse.post(url, json={"reason": "NPO"}).status_code == 403  # booking roles only
    other = make_user(owner, "kittipong2", "surgeon", "SUR-URO")
    assert other.post(url, json={"reason": "NPO"}).status_code == 403
    assert owner.post(url, json={"reason": "OTHER"}).status_code == 400  # say what "other" is
    assert owner.post(url, json={"reason": "NPO", "outcome": "LATER"}).status_code == 422
    own = make_user(owner, "somchai2", "surgeon", "SUR-ORTHO")
    r = own.post(url, json={"reason": "NPO", "outcome": "POSTPONED"})
    assert r.status_code == 200 and knee["case_id"] in [p["code"] for p in r.json()["state"]["pending"]]
    pending = next(p["id"] for p in r.json()["state"]["pending"] if p["code"] == knee["case_id"])
    r = owner.post(f"/api/pending/{pending}/cancel", json={"reason": "DECLINED"})
    assert r.status_code == 200 and r.json()["result"]["reason"] == "DECLINED"
    report = owner.get("/api/reports").json()["cancellations"]
    assert report["total"] == 1 and report["waiting_given_up"] == 1


def test_status_board_and_steps_over_the_api(owner):
    assert owner.get("/board").status_code == 200
    case = owner.get("/api/state").json()["schedule"]["bookings"][0]
    nurse = make_user(owner, "nurse4", "staff", "SCR-9")  # not on this case's team: holding / recovery nurses
    r = nurse.post(f"/api/cases/{case['case_id']}/milestone", json={"step": "arrived", "time": "07:30"})
    assert r.status_code == 200 and r.json()["state"]["milestones"][case["case_id"]]["stage"] == "arrived"
    assert nurse.post(f"/api/cases/{case['case_id']}/milestone", json={"step": "lunch"}).status_code == 422
    cssd = make_user(owner, "cssd4", "cssd")
    assert cssd.post(f"/api/cases/{case['case_id']}/milestone", json={"step": "in_room"}).status_code == 403
    board = cssd.get("/api/board").json()  # every role can see the board
    room = next(r for r in board["rooms"] if r["room"] == case["or_id"])
    first = next(c for c in room["cases"] if c["case_id"] == case["case_id"])
    assert first["stage"] == "arrived" and first["procedure"] == case["procedure"]
    text = str(owner.get("/api/board").json())
    assert "HN-" not in text and "patient" not in text  # no patient identity on a screen in the corridor
    family = owner.get("/api/board?family=1").json()
    assert family["family"] and all("procedure" not in c and "surgeon" not in c for r in family["rooms"] for c in r["cases"])


def test_or_block_endpoints_need_supabase_and_the_roster_permission(owner):
    body = {"room": "OR-1", "weekdays": [0], "start": "08:00", "end": "12:00", "owner_kind": "SPECIALTY",
            "specialty": "Orthopedics"}
    assert owner.get("/api/admin/blocks").status_code == 400  # hospital data needs Supabase
    scheduler = make_user(owner, "sched9", "scheduler")
    assert scheduler.post("/api/admin/blocks", json=body).status_code == 400  # allowed, but demo mode
    nurse = make_user(owner, "nurse9b", "staff", "SCR-2")
    assert nurse.get("/api/admin/blocks").status_code == 403
    assert nurse.post("/api/admin/blocks", json=body).status_code == 403
    assert scheduler.post("/api/admin/blocks", json={**body, "owner_kind": "TEAM"}).status_code == 422
    state = owner.get("/api/state").json()
    assert state["blocks"] == [] and state["blocks_ready"] is False


def test_system_status_is_for_admins_only(owner, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-never-shown")
    assert owner.get("/status").status_code == 200
    status = owner.get("/api/system/status").json()
    assert status["mode"] == "demo" and status["migrations"] == [] and status["settings"]
    assert "sk-never-shown" not in str(status) and OWNER[1] not in str(status)
    r = owner.post("/api/system/migrate", json={"confirm": True})
    assert r.status_code == 400 and "Supabase" in r.json()["detail"]  # nothing to run in demo mode
    for role, staff in [("scheduler", None), ("staff", "SCR-2"), ("surgeon", "SUR-GEN")]:
        other = make_user(owner, f"st-{role}", role, staff)
        assert other.get("/api/system/status").status_code == 403
        assert other.post("/api/system/migrate", json={"confirm": True}).status_code == 403
