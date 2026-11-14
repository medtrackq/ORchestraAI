"""A practice system (APP_ENVIRONMENT=staging): marked on every page, notification and the installed app, and
an admin can start a new training round with fresh demo data while accounts stay.

The database tests run against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

import json

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import environment as env
from app.admin_store import Inventory
from app.auth import DbUserStore
from app.main import accounts, app
from app.store import Forbidden, StoreError
from test_app import OWNER, fresh, make_user, owner, session  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, _request, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)
from test_notify import SUB, USERS, FakePush, notifier

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")


@pytest.fixture()
def practice(monkeypatch):
    monkeypatch.setenv("APP_ENVIRONMENT", "staging")


def test_which_system_and_how_it_is_marked(monkeypatch):
    monkeypatch.delenv("APP_ENVIRONMENT", raising=False)
    assert not env.is_practice() and env.title("ย้ายเคส") == "ย้ายเคส"
    for value in ("staging", "Training", " ซ้อม "):
        monkeypatch.setenv("APP_ENVIRONMENT", value)
        assert env.is_practice()
    assert env.title("ย้ายเคส") == "[ซ้อม] ย้ายเคส" and env.title(env.title("x")) == "[ซ้อม] x"
    page = env.mark_page(b'<html><head><title>ORchestraAI</title></head><body class="x"><main></main></body></html>').decode()
    assert "<title>[ซ้อม] ORchestraAI</title>" in page
    assert page.index('<body class="x">') < page.index('data-env="practice"') < page.index("<main>")
    named = json.loads(env.manifest(b'{"name": "ORchestraAI", "short_name": "ORchestra"}'))
    assert named == {"name": "ORchestraAI (ซ้อม)", "short_name": "ORchestra ซ้อม"}
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    assert not env.is_practice()


def test_every_page_carries_the_strip(practice, owner):
    public = TestClient(app, base_url="https://testserver")
    for path in ("/login", "/help", "/pilot"):
        r = public.get(path)
        assert r.status_code == 200 and "ระบบซ้อม · ข้อมูลไม่จริง" in r.text and "<title>[ซ้อม]" in r.text, path
        assert r.headers["content-security-policy"] and int(r.headers["content-length"]) == len(r.content)
    for path in ("/", "/admin", "/status", "/print", "/board", "/security"):
        assert "data-env=\"practice\"" in owner.get(path).text, path
    assert "ระบบซ้อม" not in owner.get("/api/state").text  # data stays as it is
    assert public.get("/manifest.webmanifest").json()["name"].endswith("(ซ้อม)")
    status = owner.get("/api/system/status").json()
    assert any(c["label"] == "นี่คือระบบซ้อม" for c in status["checks"])
    assert next(s for s in status["settings"] if s["key"] == "APP_ENVIRONMENT")["value"].startswith("ระบบซ้อม")


def test_the_real_system_is_unchanged(monkeypatch, owner):
    monkeypatch.delenv("APP_ENVIRONMENT", raising=False)
    page = TestClient(app, base_url="https://testserver").get("/login")
    assert "data-env" not in page.text and "[ซ้อม]" not in page.text
    assert "ซ้อม" not in TestClient(app, base_url="https://testserver").get("/manifest.webmanifest").json()["name"]
    assert not any(c["label"] == "นี่คือระบบซ้อม" for c in owner.get("/api/system/status").json()["checks"])
    assert owner.post("/api/admin/practice/reset", json={"confirm": env.RESET_PHRASE}).status_code in (400, 403)


def test_notifications_from_a_practice_system_say_so(practice):
    push = FakePush()
    n = notifier(push)
    n.subscribe(USERS[1], SUB, "Phone")
    assert n.test(USERS[1]) == ["sent"]
    assert push.sent[-1][1]["title"].startswith("[ซ้อม] ")


# ------------------------------------------------------------------ starting a new training round

@needs_db
def test_a_new_round_starts_from_fresh_demo_data_and_keeps_accounts(store, db_url, practice, monkeypatch):
    users = DbUserStore(store.db)
    napat = next(p["id"] for p in store.people() if p["name"].startswith("Dr.Napat"))
    trainee = users.create("napat.trainee", "Napat (ฝึก)", "surgeon", napat, "Temporary-Pass-77")
    users.create("nurse.trainee", "Nurse (ฝึก)", "staff", None, "Temporary-Pass-77")
    day = _tomorrow(store)
    assert store.slot_check(_request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "TEST-001"), "09:00",
                            commit=True, day=day)["booked"]
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("""INSERT INTO app_feedback (kind, message, user_id, username, display_name, role)
                        VALUES ('IDEA', 'ปุ่มใหญ่ขึ้น', 'u-n1', 'nurse', 'Nurse', 'staff')""")
    admin = Inventory(store)

    with pytest.raises(StoreError, match=env.RESET_PHRASE):
        env.reset_practice(store, admin, "ล้างข้อมูล")
    out = env.reset_practice(store, admin, env.RESET_PHRASE, actor={"name": "Admin", "role": "admin"})
    assert out["booked"] == 10 and out["relinked_users"] == 1 and out["unlinked_users"] == 0
    assert _rows(db_url, "SELECT count(*) FROM surgery_requests WHERE patient_hn = 'TEST-001'") == [(0,)]
    assert len(store.state()["schedule"]["bookings"]) == 10
    new_napat = next(p["id"] for p in store.people() if p["name"].startswith("Dr.Napat"))
    assert new_napat != napat and users.get(trainee.user_id).staff_id == new_napat  # linked to the new record
    assert _rows(db_url, "SELECT count(*) FROM app_users") == [(2,)]
    assert _rows(db_url, "SELECT message FROM app_feedback") == [("ปุ่มใหญ่ขึ้น",)]  # what trainees reported stays
    titles = [t for (t,) in _rows(db_url, "SELECT title FROM orchestra_activity")]
    assert titles and all("ข้อมูลตัวอย่าง" in t for t in titles)  # the round before is gone

    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    with pytest.raises(Forbidden):
        env.reset_practice(store, admin, env.RESET_PHRASE)


@needs_db
def test_only_admins_start_a_new_round(store, practice, owner, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(accounts, "store", DbUserStore(store.db))
    status = owner.get("/api/admin/demo-data").json()
    assert status["practice"] and status["practice_phrase"] == env.RESET_PHRASE
    scheduler = make_user(owner, "sched5", "scheduler")
    assert scheduler.post("/api/admin/practice/reset", json={"confirm": env.RESET_PHRASE}).status_code == 403
    assert owner.post("/api/admin/practice/reset", json={"confirm": "x"}).status_code == 400
    r = owner.post("/api/admin/practice/reset", json={"confirm": env.RESET_PHRASE})
    assert r.status_code == 200 and r.json()["booked"] == 10
