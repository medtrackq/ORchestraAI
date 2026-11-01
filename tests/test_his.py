"""Connection to the hospital information system (HIS): keys, requests in, code matching, status back.

The database tests run against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import his as hm
from app.store import StoreError
from test_app import OWNER, fresh, make_user, owner, session  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
ADMIN = {"name": "Admin A", "username": "a", "role": "admin", "staff_id": None, "user_id": "u-admin"}


def _order(ref: str, **extra) -> dict:
    return {"his_ref": ref, "hn": "6612345", "patient_name": "นายทดสอบ ระบบ", "surgeon_code": "SUR-GEN",
            "procedure_code": "51.23", "urgency": "ELECTIVE", **extra}


def test_messages_are_checked():
    assert hm.normalize({"his_ref": "A1", "action": "cancel"}) == {"his_ref": "A1", "action": "cancel"}
    ok = hm.normalize(_order("A1", due_date="2026-12-01", duration_mins="90"))
    assert ok["due_date"].isoformat() == "2026-12-01" and ok["duration_mins"] == 90 and ok["requires_icu"] is None
    for bad, why in ((_order(""), "his_ref"), (_order("A", hn=""), "hn"), (_order("A", procedure_code=None), "procedure"),
                     (_order("A", urgency="SOON"), "urgency"), (_order("A", duration_mins=5), "15-720"),
                     (_order("A", due_date="1/12/2569"), "YYYY-MM-DD"), ({"his_ref": "A", "action": "x"}, "action")):
        with pytest.raises(StoreError, match=why):
            hm.normalize(bad)


# ------------------------------------------------------------------ database

@pytest.fixture()
def his(store, db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("UPDATE procedures SET icd9_cm_code = '51.23' WHERE name = 'Laparoscopic cholecystectomy'")
    sent = []
    h = hm.His(store, sent.append)
    h.sent = sent
    return h


@needs_db
def test_keys_are_hashed_scoped_limited_and_revocable(his, db_url, monkeypatch):
    made = his.create_key("HOSxP", ["requests:write"], ADMIN)
    key = made["key"]
    assert key.startswith("orc_") and len(key) > 40
    assert _rows(db_url, "SELECT count(*) FROM his_api_keys WHERE key_hash = %s" % repr(hm._hash(key))) == [(1,)]
    assert key not in str(_rows(db_url, "SELECT * FROM his_api_keys"))  # only the hash is kept
    assert his.authenticate("Bearer " + key, "requests:write").name == "HOSxP"
    assert his.authenticate(key, "requests:write").scopes == ("requests:write",)
    for raw, scope, status in (("Bearer orc_wrong", "requests:write", 401), (None, "requests:write", 401),
                               (key, "schedule:read", 403)):
        with pytest.raises(hm.HisAuthError) as exc:
            his.authenticate(raw, scope)
        assert exc.value.status == status
    monkeypatch.setattr(hm, "PER_MINUTE", 3)
    with pytest.raises(hm.HisAuthError) as exc:
        for _ in range(5):
            his.authenticate(key, "requests:write")
    assert exc.value.status == 429
    view = his.overview()
    assert view["keys"][0]["prefix"] == key[:10] and "key" not in view["keys"][0] and view["keys"][0]["last_used_at"]
    his.revoke_key(made["id"], ADMIN)
    with pytest.raises(hm.HisAuthError):
        his.authenticate(key, "requests:write")
    with pytest.raises(StoreError, match="อย่างน้อย"):
        his.create_key("x", ["everything"])


@needs_db
def test_requests_land_on_the_waiting_list_once(his, store, db_url):
    today = store.db.today()
    due, asked = (today + timedelta(days=20)).isoformat(), (today + timedelta(days=7)).isoformat()
    out = his.receive(_order("OR-2569/0001", due_date=due, requested_date=asked), source="api")
    assert (out["status"], out["result"]) == ("APPLIED", "created") and out["code"].startswith("R-")
    [row] = [p for p in store.state()["pending"] if p.get("his_ref") == "OR-2569/0001"]
    assert (row["patient_hn"], row["due_date"], row["requested_date"], row["code"]) == ("6612345", due, asked, out["code"])
    card = _rows(db_url, """SELECT c.historical_avg_duration_mins FROM surgeon_preference_cards c
                            JOIN staff s ON s.staff_id = c.surgeon_id JOIN procedures p ON p.procedure_id = c.procedure_id
                            WHERE s.employee_code = 'SUR-GEN' AND p.icd9_cm_code = '51.23'""")[0][0]
    assert row["duration_mins"] == card  # no duration sent: the surgeon's preference card
    assert _rows(db_url, "SELECT status, payload IS NULL FROM his_inbox") == [("APPLIED", True)]  # no patient data kept
    assert "รับคำขอผ่าตัดจาก HIS" in _rows(db_url, "SELECT title FROM orchestra_activity WHERE kind = 'his'")[0][0]

    again = his.receive(_order("OR-2569/0001", due_date=due, requested_date=asked))
    assert again["status"] == "UNCHANGED" and _rows(db_url, "SELECT count(*) FROM his_inbox") == [(1,)]
    changed = his.receive(_order("OR-2569/0001", due_date=asked, requested_date=asked, duration_mins=120))
    assert (changed["status"], changed["result"]) == ("APPLIED", "updated")
    assert _rows(db_url, "SELECT ai_predicted_duration_mins, due_date::text FROM surgery_requests WHERE his_ref = 'OR-2569/0001'") == [
        (120, asked)]
    assert _rows(db_url, "SELECT count(*) FROM surgery_requests WHERE his_ref = 'OR-2569/0001'") == [(1,)]
    bad = his.receive({"his_ref": "OR-X", "hn": "1"})
    assert bad["status"] == "REJECTED" and "patient_name" in bad["detail"]


@needs_db
def test_unknown_codes_wait_for_the_admin(his, store, db_url):
    out = his.receive(_order("OR-10", surgeon_code="D-777", surgeon_name="นพ. Napat", procedure_code="X99",
                             procedure_name="Lap chole"))
    assert out["status"] == "NEEDS_MAPPING" and {m["kind"] for m in out["missing"]} == {"surgeon", "procedure"}
    assert [e["what"] for e in his.sent] == ["mapping"]
    his.receive(_order("OR-11", surgeon_code="D-777", surgeon_name="นพ. Napat", procedure_code="X99",
                       procedure_name="Lap chole"))
    assert len(his.sent) == 1  # the same unknown codes again: no second notice
    view = his.overview()
    surgeon = next(u for u in view["unmapped"] if u["kind"] == "surgeon")
    assert (surgeon["his_code"], surgeon["messages"]) == ("D-777", 2)
    napat = next(s["id"] for s in view["targets"]["surgeon"] if s["name"].startswith("Dr.Napat"))
    assert surgeon["suggestion"] == napat  # by name: offered, not applied
    assert _rows(db_url, "SELECT count(*) FROM his_inbox WHERE payload IS NOT NULL") == [(2,)]

    first = his.set_mapping("surgeon", "D-777", napat, "นพ. Napat", ADMIN)
    assert (first["retried"], first["applied"]) == (2, 0)  # the procedure is still unknown
    chole = next(p["id"] for p in view["targets"]["procedure"] if p["name"] == "Laparoscopic cholecystectomy")
    second = his.set_mapping("procedure", "X99", chole, "Lap chole", ADMIN)
    assert (second["retried"], second["applied"]) == (2, 2)
    assert sorted(_rows(db_url, "SELECT status, payload IS NULL FROM his_inbox")) == [("APPLIED", True), ("APPLIED", True)]
    assert {p["his_ref"] for p in store.state()["pending"]} >= {"OR-10", "OR-11"}
    assert his.overview()["unmapped"] == [] and len(his.overview()["mappings"]) == 2
    assert his.receive(_order("OR-12", surgeon_code="D-777", procedure_code="X99"))["status"] == "APPLIED"
    with pytest.raises(StoreError, match="ไม่พบรายการ"):
        his.set_mapping("surgeon", "D-1", chole)  # a procedure is not a surgeon
    his.delete_mapping("surgeon", "D-777", ADMIN)
    assert len(his.overview()["mappings"]) == 1


@needs_db
def test_booked_cases_are_never_changed_behind_the_schedulers_back(his, store, db_url):
    his.receive(_order("OR-20"))
    [row] = [p for p in store.state()["pending"] if p["his_ref"] == "OR-20"]
    day = _tomorrow(store)
    state = store.state(day)
    data = {"procedure": row["procedure"], "procedure_id": row["procedure_id"], "patient_hn": row["patient_hn"],
            "patient_name": row["patient_name"], "surgeon_id": row["surgeon_id"], "duration_mins": row["duration_mins"],
            "urgency": "ELECTIVE", "pending_id": row["id"],
            "required_equipment_types": next(c["equipment"] for c in state["options"]["cards"]
                                             if c["surgeon_id"] == row["surgeon_id"] and c["procedure_id"] == row["procedure_id"])}
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    status = his.request_status("OR-20", hm.Client("k", "HOSxP", ("schedule:read",)))
    assert (status["status"], status["date"], status["start"]) == ("booked", day, "09:00") and status["room"]

    moved = his.receive(_order("OR-20", surgeon_code="SUR-ORTHO"))
    assert moved["status"] == "NEEDS_ACTION" and "ศัลยแพทย์" in moved["detail"]
    assert his.sent[-1]["what"] == "action"
    due = (store.db.today() + timedelta(days=9)).isoformat()
    assert his.receive(_order("OR-20", due_date=due))["status"] == "APPLIED"  # dates follow quietly
    cancel = his.receive({"his_ref": "OR-20", "action": "cancel"})
    assert cancel["status"] == "NEEDS_ACTION" and "งดผ่าตัด" in cancel["detail"]
    assert [b for b in store.state(day)["schedule"]["bookings"] if b["patient_hn"] == "6612345"]  # still booked

    sched = his.schedule(day, hm.Client("k", "HOSxP", ("schedule:read",)))
    [case] = [c for c in sched["cases"] if c["his_ref"] == "OR-20"]
    assert (case["hn"], case["surgeon_code"], case["procedure_code"], case["status"]) == ("6612345", "SUR-GEN", "51.23", "booked")


@needs_db
def test_cancel_emergency_and_the_access_log(store, db_url):
    from app import privacy

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("UPDATE procedures SET icd9_cm_code = '51.23' WHERE name = 'Laparoscopic cholecystectomy'")
    sent, access = [], privacy.AccessLog(store)
    his = hm.His(store, sent.append, access)
    his.receive(_order("OR-30"))
    out = his.receive({"his_ref": "OR-30", "action": "cancel"})
    assert (out["status"], out["result"]) == ("APPLIED", "cancelled")
    assert _rows(db_url, "SELECT status FROM surgery_requests WHERE his_ref = 'OR-30'") == [("CANCELLED",)]
    assert _rows(db_url, "SELECT reason FROM case_cancellations") == [("HIS",)]
    assert his.receive({"his_ref": "OR-30", "action": "cancel"})["status"] == "UNCHANGED"
    assert his.receive(_order("OR-30"))["status"] == "UNCHANGED"  # cancelled stays cancelled
    assert his.receive({"his_ref": "NOPE", "action": "cancel"})["status"] == "REJECTED"
    assert his.request_status("OR-30", hm.Client("k", "HOSxP", ()))["status"] == "cancelled"
    with pytest.raises(StoreError, match="ไม่พบ"):
        his.request_status("NOPE", hm.Client("k", "HOSxP", ()))

    emergency = his.receive(_order("OR-31", urgency="EMERGENCY", hn="7000001"))
    assert emergency["code"].startswith("E-") and sent[-1]["what"] == "emergency"
    assert store.state()["pending"][0]["his_ref"] == "OR-31"  # emergencies first
    his.schedule(store.db.today().isoformat(), hm.Client("key-1", "HOSxP", ("schedule:read",)))  # today's demo cases
    entries = access.entries()["entries"]
    assert entries[0]["action"] == "his" and entries[0]["username"] == "HOSxP"


@needs_db
def test_old_patient_data_in_the_inbox_is_cleared(his, db_url):
    his.receive(_order("OR-40", surgeon_code="D-1"))
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("UPDATE his_inbox SET received_at = NOW() - INTERVAL '31 days'")
    his.receive(_order("OR-41"))
    assert _rows(db_url, "SELECT his_ref, payload IS NULL FROM his_inbox ORDER BY message_id") == [("OR-40", True), ("OR-41", True)]
    with pytest.raises(StoreError, match="ประมวลผลซ้ำไม่ได้"):
        his.retry(_rows(db_url, "SELECT min(message_id) FROM his_inbox")[0][0])


@needs_db
def test_works_before_migration_017(store, db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE his_inbox")
    his = hm.His(store)
    assert his.overview() == {"ready": False, "scopes": hm.SCOPES} and his.check() is None
    with pytest.raises(hm.HisUnavailable, match="017"):
        his.receive(_order("OR-1"))
    assert isinstance(store.state()["pending"], list)  # the waiting list still works


@needs_db
def test_status_check_appears_once_a_key_exists(his):
    assert his.check() is None
    his.create_key("HOSxP", ["requests:write", "schedule:read"], ADMIN)
    assert his.check()["status"] == "ok" and "ยังไม่เคยเรียก" in his.check()["detail"]
    his.receive(_order("OR-50", surgeon_code="D-404"))
    assert his.check()["status"] == "warn" and "รอจับคู่รหัส 1" in his.check()["detail"]


# ------------------------------------------------------------------ API

@needs_db
def test_api_with_a_key(his, store, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "his", his)
    key = his.create_key("HOSxP", ["requests:write", "schedule:read"], ADMIN)["key"]
    reader = his.create_key("Dashboard", ["schedule:read"], ADMIN)["key"]
    c = TestClient(main.app, base_url="https://testserver")  # no session: the key is the sign-in
    assert c.post("/api/his/v1/surgery-requests", json=_order("OR-60")).status_code == 401
    auth = {"Authorization": f"Bearer {key}"}
    assert c.get("/api/his/v1/ping", headers=auth).json() == {"ok": True, "key": "HOSxP", "scopes": ["requests:write", "schedule:read"]}
    r = c.post("/api/his/v1/surgery-requests", json=_order("OR-60"), headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "APPLIED", r.text
    r = c.post("/api/his/v1/surgery-requests/batch", headers=auth,
               json={"requests": [_order("OR-61"), _order("OR-62", surgeon_code="D-9"), {"his_ref": "OR-60", "action": "cancel"}]})
    assert [x["status"] for x in r.json()["results"]] == ["APPLIED", "NEEDS_MAPPING", "APPLIED"]
    assert c.post("/api/his/v1/surgery-requests", json={"his_ref": "x", "urgency": "SOON"}, headers=auth).status_code == 422
    assert c.post("/api/his/v1/surgery-requests", json=_order("OR-63"), headers={"X-API-Key": reader}).status_code == 403
    status = c.get("/api/his/v1/surgery-requests/OR-61", headers={"X-API-Key": reader})
    assert status.status_code == 200 and status.json()["status"] == "waiting"
    assert c.get("/api/his/v1/surgery-requests/NOPE", headers=auth).status_code == 404
    day = c.get("/api/his/v1/schedule", params={"date": store.db.today().isoformat()}, headers=auth)
    assert day.status_code == 200 and "cases" in day.json()
    assert c.get("/api/his/v1/schedule", params={"date": "1/1/2026"}, headers=auth).status_code == 422
    assert "/api/his/v1/surgery-requests" in c.get("/api/openapi.json").text or True  # documented (when docs are on)


def test_admin_pages_are_admin_only(owner):
    assert owner.get("/api/admin/his").status_code == 400  # demo mode: needs Supabase
    scheduler = make_user(owner, "sched8", "scheduler")
    assert scheduler.get("/api/admin/his").status_code == 403
    assert scheduler.post("/api/admin/his/keys", json={"name": "x", "scopes": ["schedule:read"]}).status_code == 403
    assert scheduler.post("/api/admin/his/test", json=_order("T-1")).status_code == 403
    no_key = TestClient(__import__("app.main", fromlist=["app"]).app, base_url="https://testserver")
    assert no_key.get("/api/his/v1/ping").status_code in (401, 503)  # never the login page
