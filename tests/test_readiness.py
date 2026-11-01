"""Pre-operative readiness checklist: rules, reminders, permissions and storage.

The rule and message tests need no database; the store tests run on the demo
store, and the Supabase ones against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app import readiness as rd
from app.notify import build_messages, default_prefs
from app.store import Forbidden, ScheduleStore, StoreError
from test_db import ADMIN_URL, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

NURSE = {"name": "Nurse A", "username": "n", "role": "staff", "staff_id": "SCR-1", "user_id": "u-nurse"}
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")


# ------------------------------------------------------------------ rules

def test_checklist_status():
    fresh = rd.summarize({}, needs_blood=False)
    assert fresh["status"] == "pending" and fresh["items"]["blood"] == {
        "status": "NA", "note": None, "by": None, "at": None, "auto": True}  # blood only when the procedure needs it
    assert fresh["pending"] == ["consent", "anesthesia", "labs", "npo", "site"]
    assert rd.summarize({}, needs_blood=True)["pending"][-2:] == ["blood", "site"]
    done = {k: {"status": "DONE"} for k in rd.LABELS}
    assert rd.summarize(done, True)["status"] == "ready" and rd.summarize(done, True)["done"] == 6
    flagged = {**done, "npo": {"status": "PROBLEM", "note": "ดื่มน้ำ 07:00"}}
    s = rd.summarize(flagged, True)
    assert (s["status"], s["problems"], s["done"]) == ("problem", ["npo"], 5)
    assert rd.missing_text(rd.summarize({"consent": {"status": "PROBLEM"}}, False)) == \
        "มีปัญหา: ยินยอม · ยังไม่ได้: วิสัญญี, แล็บ, NPO, Site"
    for bad, message in [({"item": "xray", "status": "DONE"}, "รายการ"), ({"item": "npo", "status": "OK"}, "สถานะ"),
                         ({"item": "npo", "status": "PROBLEM", "note": " "}, "ปัญหาอะไร")]:
        with pytest.raises(StoreError, match=message):
            rd.parse_update(bad)
    assert rd.parse_update({"item": "npo", "status": "problem", "note": " ดื่มน้ำ "}) == ("npo", "PROBLEM", "ดื่มน้ำ")


def _user(uid, role, staff_id=None):
    return SimpleNamespace(user_id=uid, role=role, staff_id=staff_id)


def test_messages_go_to_schedulers_and_the_cases_surgeon_without_notes():
    users = [_user("a", "admin"), _user("s", "scheduler"), _user("d1", "surgeon", "SUR-1"), _user("d2", "surgeon", "SUR-2"),
             _user("n", "staff", "SCR-1"), _user("c", "cssd")]
    event = rd.problem_event(datetime(2026, 11, 2).date(), "R-AB12CD", "npo", "08:00-10:00 ห้อง OR-1", "SUR-1",
                             {"user_id": "s"})
    messages = build_messages(event, users)
    assert sorted(m.user_id for m in messages) == ["a", "d1"]  # not the person who flagged it
    m = messages[0]
    assert m.title == "R-AB12CD มีปัญหาความพร้อม: งดน้ำงดอาหาร" and m.url == "/?date=2026-11-02#ready"
    assert "08:00-10:00 ห้อง OR-1" in m.body

    digest = rd.digest_event({"date": "2026-11-03", "cases": [
        {"case_id": "R-1", "start": "08:00", "or_id": "OR-1", "surgeon_id": "SUR-1",
         "readiness": rd.summarize({}, False)},
        {"case_id": "R-2", "start": "10:00", "or_id": "OR-2", "surgeon_id": "SUR-2",
         "readiness": rd.summarize({"npo": {"status": "PROBLEM", "note": "secret"}}, False)}]})
    by_user = {m.user_id: m for m in build_messages(digest, users)}
    assert set(by_user) == {"a", "s", "d1", "d2"}
    assert by_user["s"].title == "เคสวันที่ 2026-11-03 ยังไม่พร้อม 2 เคส" and len(by_user["s"].lines) == 2
    assert by_user["d1"].lines == ["R-1 08:00 ห้อง OR-1: ยังไม่ได้: ยินยอม, วิสัญญี, แล็บ, NPO, Site"]
    assert all("secret" not in m.body for m in by_user.values())
    assert default_prefs("surgeon")["readiness"] and not default_prefs("staff")["readiness"]


def test_daily_reminder_goes_out_once_after_the_hour():
    sent, claims = [], set()

    class Fake:
        db = SimpleNamespace(tz=None)

        def readiness_next(self):
            return {"date": "2026-11-03", "total": 2, "cases": [
                {"case_id": "R-1", "start": "08:00", "or_id": "OR-1", "surgeon_id": None,
                 "readiness": rd.summarize({}, False)}]}

        def claim_digest(self, day):
            if day in claims:
                return False
            claims.add(day)
            return True

    timer = rd.DigestTimer(Fake(), sent.append, hour=14)
    assert not timer.tick(datetime(2026, 11, 2, 13, 59))
    assert timer.tick(datetime(2026, 11, 2, 14, 0)) and not timer.tick(datetime(2026, 11, 2, 15, 0))
    assert len(sent) == 1 and sent[0]["type"] == "readiness_digest" and sent[0]["day"] == "2026-11-03"
    other = rd.DigestTimer(Fake(), sent.append, hour=14)  # another worker / a restart: already claimed
    assert not other.tick(datetime(2026, 11, 2, 16, 0)) and len(sent) == 1


def test_digest_hour_setting(monkeypatch):
    monkeypatch.delenv("READINESS_DIGEST_HOUR", raising=False)
    assert rd.digest_hour() == 14
    for raw, hour in [("off", None), ("7", 7), ("30", 23), ("x", 14)]:
        monkeypatch.setenv("READINESS_DIGEST_HOUR", raw)
        assert rd.digest_hour() == hour


# ------------------------------------------------------------- demo store

def test_demo_store_checklist_and_permissions():
    s = ScheduleStore(workers=2, time_limit_s=10)
    events = []
    s.listener = events.append
    state = s.state()
    assert state["readiness_ready"] and set(state["readiness"]) == set(state["schedule"]["bookings"][i]["case_id"]
                                                                      for i in range(len(state["schedule"]["bookings"])))
    knee = next(b for b in state["schedule"]["bookings"] if b["procedure"].startswith("Total knee"))
    hernia = next(b for b in state["schedule"]["bookings"] if b["procedure"] == "Open inguinal hernia repair")
    assert state["readiness"][knee["case_id"]]["items"]["blood"]["status"] == "PENDING"
    assert state["readiness"][hernia["case_id"]]["items"]["blood"]["status"] == "NA"

    for item in ("consent", "anesthesia", "labs", "npo", "blood", "site"):
        out = s.readiness_update(knee["case_id"], {"item": item, "status": "DONE"}, actor=NURSE)
    assert out["readiness"]["status"] == "ready" and out["readiness"]["items"]["npo"]["by"] == "Nurse A (staff)"
    out = s.readiness_update(knee["case_id"], {"item": "npo", "status": "PROBLEM", "note": "ดื่มน้ำ"}, actor=NURSE)
    assert out["readiness"]["status"] == "problem" and len(events) == 1 and events[0]["type"] == "readiness"
    s.readiness_update(knee["case_id"], {"item": "npo", "status": "PROBLEM", "note": "ดื่มน้ำ"}, actor=NURSE)
    assert len(events) == 1  # the same problem again is not news
    out = s.readiness_update(knee["case_id"], {"item": "npo", "status": "PENDING"}, actor=NURSE)
    assert out["readiness"]["pending"] == ["npo"]

    other = {"name": "Dr X", "role": "surgeon", "staff_id": "SUR-URO"}
    with pytest.raises(Forbidden):
        s.readiness_update(knee["case_id"], {"item": "consent", "status": "DONE"}, actor=other)
    with pytest.raises(StoreError, match="ไม่พบเคส"):
        s.readiness_update("NOPE", {"item": "consent", "status": "DONE"}, actor=NURSE)
    assert s.claim_digest("2026-11-02") and not s.claim_digest("2026-11-02")


# ------------------------------------------------------- Supabase store

@needs_db
def test_checklist_is_saved_and_shown_with_the_schedule(store, db_url):
    events = []
    store.listener = events.append
    state = store.state()
    assert state["readiness_ready"] and len(state["readiness"]) == len(state["schedule"]["bookings"])
    knee = next(b for b in state["schedule"]["bookings"] if b["procedure"].startswith("Total knee"))
    chole = next(b for b in state["schedule"]["bookings"] if b["procedure"] == "Laparoscopic cholecystectomy")
    assert state["readiness"][knee["case_id"]]["items"]["blood"]["status"] == "PENDING"  # seeded: needs blood
    assert state["readiness"][chole["case_id"]]["items"]["blood"]["auto"]

    store.readiness_update(knee["case_id"], {"item": "consent", "status": "DONE"}, actor=NURSE)
    out = store.readiness_update(knee["case_id"], {"item": "labs", "status": "PROBLEM", "note": "K 2.9 รอแก้"},
                                 actor=NURSE)
    assert out["readiness"]["status"] == "problem"
    item = store.state()["readiness"][knee["case_id"]]["items"]["labs"]
    assert (item["status"], item["note"], item["by"]) == ("PROBLEM", "K 2.9 รอแก้", "Nurse A (staff)")
    assert [e["item"] for e in events if e["type"] == "readiness"] == ["ผลแล็บ/ผลตรวจก่อนผ่าตัด"]
    assert "K 2.9" not in str(events)  # the note never leaves the app
    store.readiness_update(knee["case_id"], {"item": "consent", "status": "PENDING"}, actor=NURSE)
    assert _rows(db_url, "SELECT item, status FROM case_readiness ORDER BY item") == [("labs", "PROBLEM")]

    surgeon = {"name": "Dr", "role": "surgeon", "staff_id": "not-the-surgeon"}
    with pytest.raises(Forbidden):
        store.readiness_update(knee["case_id"], {"item": "consent", "status": "DONE"}, actor=surgeon)


@needs_db
def test_next_day_reminder_lists_cases_that_are_not_ready(store):
    from test_db import _request

    assert store.state()["readiness_next"] is None and store.readiness_next() is None  # nothing booked ahead yet
    day = _tomorrow(store)
    assert store.slot_check(_request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-RD-1"),
                            "09:00", commit=True, day=day)["booked"]
    nxt = store.state()["readiness_next"]
    assert nxt["date"] == day and nxt["total"] == 1 and len(nxt["cases"]) == 1
    case = nxt["cases"][0]
    assert (case["start"], case["surgeon"]) == ("09:00", "Dr.Napat (General)")
    assert case["readiness"]["pending"] == ["consent", "anesthesia", "labs", "npo", "site"]
    assert store.readiness_next()["date"] == day

    sent = []
    timer = rd.DigestTimer(store, sent.append, hour=0)
    assert timer.tick() and not timer.tick() and not rd.DigestTimer(store, sent.append, hour=0).tick()
    assert sent[0]["cases"][0]["case"] == case["case_id"]

    for item in case["readiness"]["pending"]:
        store.readiness_update(case["case_id"], {"item": item, "status": "DONE"}, day=day, actor=NURSE)
    after = store.state()["readiness_next"]
    assert after["date"] == day and after["cases"] == []  # all ready: nothing to remind


@needs_db
def test_works_before_migration_009(db_url):
    import psycopg

    from app.db_store import DatabaseStore

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE case_readiness")
        conn.execute("ALTER TABLE procedures DROP COLUMN requires_blood")
    s = DatabaseStore(db_url, workers=2, time_limit_s=10)
    s.seed_demo()
    state = s.state()
    assert state["readiness_ready"] is False and state["readiness"] == {} and state["readiness_next"] is None
    case = state["schedule"]["bookings"][0]["case_id"]
    with pytest.raises(StoreError, match="20261108000000_case_readiness.sql"):
        s.readiness_update(case, {"item": "consent", "status": "DONE"}, actor=NURSE)
    assert s.readiness_next() is None


@needs_db
def test_procedures_carry_the_blood_flag(store):
    from app.admin_store import Inventory

    admin = Inventory(store)
    proc = next(p for p in admin.procedures()["procedures"] if p["name"] == "Laparoscopic cholecystectomy")
    assert admin.procedures()["blood_ready"] and proc["blood"] is False
    admin.save_procedure({**proc, "blood": True}, procedure_id=proc["id"])
    assert next(p for p in admin.procedures()["procedures"] if p["id"] == proc["id"])["blood"]
    admin.save_procedure({k: v for k, v in proc.items() if k != "blood"}, procedure_id=proc["id"])  # left out: kept
    assert next(p for p in admin.procedures()["procedures"] if p["id"] == proc["id"])["blood"]
    state = store.state()
    chole = next(b for b in state["schedule"]["bookings"] if b["procedure"] == "Laparoscopic cholecystectomy")
    assert state["readiness"][chole["case_id"]]["items"]["blood"]["status"] == "PENDING"
    assert next(p for p in state["options"]["procedures"] if p["id"] == proc["id"])["blood"]
