"""Problem reports from users and errors of the system, for the admin in one place.

The database tests run against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

import logging

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import feedback as fb
from app.auth import User
from app.main import app
from app.main import feedback as live
from app.notify import build_messages
from app.store import StoreError
from test_app import OWNER, fresh, make_user, owner, session  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, _rows, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
NURSE = User("u-n1", "nurse1", "Nurse One", "staff", "N1")
ADMIN = User("u-a1", "admin1", "Admin One", "admin")


@pytest.fixture(autouse=True)
def clean():
    for f in (live,):
        f._memory.clear()
        f._errors.clear()
        f._sent.clear()
        f._last_notice = 0.0
    yield


# ------------------------------------------------------------------ rules

def test_patient_identifiers_are_scrubbed():
    text = "เคส HN 6612345 ของ นายสมชาย ใจดี โทร 081-234-5678 เลข 1-2345-67890-12-3 ขึ้น error"
    out = fb.scrub(text, 2000)
    for secret in ("6612345", "สมชาย", "081-234-5678", "67890"):
        assert secret not in out, out
    assert "error" in out and fb.scrub("a\x00b" + "x" * 50, 10) == "a b" + "x" * 7


def test_page_keeps_path_day_and_tab_only():
    assert fb.clean_page("https://x/?date=2026-11-02#ready") == "/?date=2026-11-02#ready"
    assert fb.clean_page("/admin?hn=123&date=bad#io") == "/admin#io"
    assert fb.clean_page("/reports#<script>") == "/reports"
    assert fb.clean_page(None) is None


def test_the_same_error_counts_once():
    a = fb.fingerprint("server", "POST /api/cases/R-1A2B/cancel", "KeyError: 123")
    b = fb.fingerprint("server", "POST /api/cases/R-9F8E/cancel", "KeyError: 456")
    assert a != b  # case codes differ in letters
    assert fb.fingerprint("job", "db", "timeout after 15 s") == fb.fingerprint("job", "db", "timeout after 30 s")


def test_notifications_carry_no_text_people_typed():
    users = [ADMIN, NURSE, User("u-s", "sch", "Sch", "scheduler")]
    [m] = build_messages({"type": "feedback", "kind": "IDEA", "role": "staff", "page": "/", "actor_id": "u-n1"}, users)
    assert (m.user_id, m.kind, m.title, m.url) == ("u-a1", "feedback", "มีผู้ใช้ข้อเสนอแนะใหม่", "/status#feedback")
    assert m.lines[0] == "จากวิสัญญี / พยาบาล · หน้า /"
    [e] = build_messages({"type": "system_error", "source": "browser", "where": "/:12"}, users)
    assert e.user_id == "u-a1" and e.lines[0] == "หน้าเว็บ: /:12"


# ------------------------------------------------------------------ in memory (demo mode)

def test_reports_errors_and_notices_in_memory(monkeypatch):
    sent = []
    f = fb.Feedback(type("Demo", (), {"mode": "demo"})(), sent.append)
    out = f.submit(NURSE, "PROBLEM", "กดจองแล้วค้าง HN 1234567", "/?date=2026-11-02", "Mozilla/5.0")
    assert out["kept"] == "memory" and out["id"] < 0
    [mine] = f.mine(NURSE)
    assert mine["status"] == "NEW" and "1234567" not in mine["message"] and "user_id" not in mine
    assert sent == [{"type": "feedback", "kind": "PROBLEM", "role": "staff", "page": "/?date=2026-11-02", "actor_id": "u-n1"}]
    with pytest.raises(StoreError, match="รายละเอียด"):
        f.submit(NURSE, "PROBLEM", " ok ")
    with pytest.raises(StoreError, match="ประเภท"):
        f.submit(NURSE, "OTHER", "something")
    monkeypatch.setattr(fb, "PER_HOUR", 1)
    with pytest.raises(StoreError, match="หลายครั้ง"):
        f.submit(NURSE, "IDEA", "อีกเรื่อง")

    assert f.record_error("browser", "/:10", "TypeError: x is undefined", user=NURSE, page="/")
    assert f.record_error("browser", "/:10", "TypeError: x is undefined", user=NURSE, page="/")
    view = f.overview()
    [err] = view["errors"]
    assert (err["occurrences"], err["status"], view["new_reports"], view["new_errors"]) == (2, "NEW", 1, 1)
    assert [e["type"] for e in sent] == ["feedback", "system_error"]  # the second time is not news
    f.update("error", err["id"], "DONE", None)
    assert f.overview()["errors"] == []
    f._last_notice = 0.0
    f.record_error("browser", "/:10", "TypeError: x is undefined", user=NURSE)  # came back after the fix
    [back] = f.overview()["errors"]
    assert back["reopened"] and back["occurrences"] == 3 and sent[-1]["type"] == "system_error"

    f.update("report", out["id"], "DONE", "แก้แล้วในเวอร์ชันใหม่ HN 7654321", {"name": "Admin"})
    [mine] = f.mine(NURSE)
    assert mine["status"] == "DONE" and mine["admin_note"].startswith("แก้แล้ว") and "7654321" not in mine["admin_note"]
    assert f.overview()["reports"] == [] and len(f.overview(show_done=True)["reports"]) == 1
    with pytest.raises(StoreError, match="ไม่พบ"):
        f.update("report", 999, "SEEN", None)
    with pytest.raises(StoreError, match="สถานะ"):
        f.update("error", back["id"], "SEEN", None)


def test_logged_errors_are_listed_and_never_loop():
    logging.getLogger("orchestra.notify").error("notification event failed", exc_info=ValueError("bad 1234567"))
    [err] = live.overview()["errors"]
    assert (err["source"], err["where_at"]) == ("job", "notify") and "ValueError" in err["message"]
    assert "1234567" not in err["message"]
    live._busy.on = True  # an error while recording an error is dropped, not recorded again
    assert live.record_error("server", "x", "y") is False
    live._busy.on = False


# ------------------------------------------------------------------ API

def test_everyone_can_report_and_only_admins_read(owner):
    nurse = make_user(owner, "nurse9", "staff")
    r = nurse.post("/api/feedback", json={"kind": "QUESTION", "message": "ปุ่มพิมพ์อยู่ตรงไหน", "page": "/print?date=2026-11-02"})
    assert r.status_code == 200, r.text
    assert r.json()["mine"][0]["kind_label"] == "สอบถาม" and r.json()["mine"][0]["page"] == "/print?date=2026-11-02"
    assert nurse.post("/api/feedback", json={"kind": "PROBLEM", "message": ""}).status_code == 422
    assert nurse.post("/api/feedback", json={"kind": "PROBLEM", "message": "ab"}).status_code == 400
    assert nurse.post("/api/client-errors", json={"message": "TypeError: boom", "where": "/:1", "page": "/"}).json() == {"recorded": True}
    assert nurse.get("/api/feedback").status_code == 403
    assert nurse.patch("/api/feedback/report/1", json={"status": "DONE"}).status_code == 403

    view = owner.get("/api/feedback").json()
    [report] = view["reports"]
    assert (report["display_name"], report["role"], view["new_errors"]) == ("Nurse9", "staff", 1)
    r = owner.patch(f"/api/feedback/report/{report['id']}", json={"status": "SEEN", "note": "กำลังดูให้"})
    assert r.status_code == 200 and r.json()["overview"]["reports"][0]["status"] == "SEEN"
    assert nurse.get("/api/feedback/mine").json()["mine"][0]["admin_note"] == "กำลังดูให้"
    error_id = view["errors"][0]["id"]
    assert owner.patch(f"/api/feedback/error/{error_id}", json={"status": "SEEN"}).status_code == 400
    assert owner.patch(f"/api/feedback/error/{error_id}", json={"status": "DONE"}).status_code == 200
    check = next(c for c in owner.get("/api/system/status").json()["checks"] if c["link"] == "#feedback")
    assert check["status"] == "ok" and "รับทราบแล้ว รอแก้ 1 เรื่อง" in check["detail"]  # seen: open, but not news

    assert owner.get("/feedback.js").headers["content-type"].startswith("text/javascript")
    assert TestClient(app, base_url="https://testserver").get("/feedback.js").status_code == 200  # the manual loads it too


def test_an_unexpected_server_error_is_listed(owner, monkeypatch):
    from app.main import store

    client = TestClient(app, base_url="https://testserver", raise_server_exceptions=False)
    client.cookies.update(owner.cookies)

    def broken(*a, **k):
        raise RuntimeError("cannot read HN 1234567")

    monkeypatch.setattr(store, "state", broken)
    r = client.get("/api/state")
    assert r.status_code == 500 and "ผู้ดูแลระบบได้รับบันทึกแล้ว" in r.json()["detail"]
    [err] = live.overview()["errors"]
    assert (err["source"], err["where_at"], err["last_user"]) == ("server", "GET /api/state", "orchestra")
    assert "RuntimeError" in err["message"] and "1234567" not in err["message"]


# ------------------------------------------------------------------ database

@needs_db
def test_reports_and_errors_in_the_database(store, db_url):
    sent = []
    f = fb.Feedback(store, sent.append)
    assert f.ready()
    out = f.submit(NURSE, "PROBLEM", "ตารางไม่โหลด HN 1234567", "/?date=2026-11-02#ready", "Mozilla/5.0")
    assert out["kept"] == "database" and out["id"] > 0
    assert _rows(db_url, "SELECT kind, page, role, status FROM app_feedback") == [("PROBLEM", "/?date=2026-11-02#ready", "staff", "NEW")]
    assert "1234567" not in _rows(db_url, "SELECT message FROM app_feedback")[0][0]
    assert f.mine(NURSE)[0]["id"] == out["id"]

    for _ in range(3):
        f.record_error("server", "GET /api/state", "KeyError: 'x'", user=NURSE)
    view = f.overview()
    [err] = view["errors"]
    assert (err["occurrences"], view["new_reports"], view["new_errors"]) == (3, 1, 1)
    assert [e["type"] for e in sent] == ["feedback", "system_error"]
    f.update("error", err["id"], "DONE", "แก้แล้ว")
    f.update("report", out["id"], "DONE", "ขอบคุณครับ", {"name": "Admin"})
    assert f.overview()["reports"] == [] and f.overview()["errors"] == []
    with store._transaction(write=False) as conn:
        assert f.counts(conn) == {"reports": 0, "errors": 0}
    f._last_notice = 0.0
    f.record_error("server", "GET /api/state", "KeyError: 'x'")
    [back] = f.overview()["errors"]
    assert back["reopened"] and back["occurrences"] == 4 and sent[-1]["type"] == "system_error"
    assert _rows(db_url, "SELECT handled_by, admin_note FROM app_feedback") == [("Admin", "ขอบคุณครับ")]


@needs_db
def test_before_migration_016_and_when_the_database_is_down(store, db_url, monkeypatch):
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE app_feedback")
        conn.execute("DROP TABLE app_errors")
    f = fb.Feedback(store)
    assert not f.ready()
    assert f.submit(NURSE, "IDEA", "อยากให้มีปุ่มพิมพ์ใหญ่ขึ้น")["kept"] == "memory"
    view = f.overview()
    assert view["kept_in_memory"] and len(view["reports"]) == 1

    def down():
        raise RuntimeError("database is down")

    g = fb.Feedback(store)
    monkeypatch.setattr(store.db, "connect", down)
    assert g.record_error("job", "db", "connection failed") and g._errors
    assert not g._db_ok()  # no slow retries for a minute
