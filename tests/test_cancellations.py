"""Calling off cases with a reason, and the cancellation part of the management report.

Rule tests need no database; store tests run on the demo store, and the
Supabase ones against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import cancellations as cx
from app.notify import build_messages
from app.store import Forbidden, ScheduleStore, StoreError
from test_db import ADMIN_URL, _request, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

ADMIN = {"name": "Admin A", "username": "a", "role": "admin", "staff_id": None, "user_id": "u-admin"}
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")


# ------------------------------------------------------------------ rules

def test_reasons_and_input():
    keys = [o["key"] for o in cx.options()]
    assert "NPO" in keys and "OTHER" in keys and "EMERGENCY" not in keys  # system reasons are not offered
    assert {o["group"] for o in cx.options()} == {"patient", "hospital", "other"}
    assert cx.parse({"reason": "npo"}) == ("NPO", None, "POSTPONED")
    assert cx.parse({"reason": "BED", "outcome": "cancelled", "note": " ICU เต็ม "}) == ("BED", "ICU เต็ม", "CANCELLED")
    for bad, message in [({}, "เลือกเหตุผล"), ({"reason": "EMERGENCY"}, "เลือกเหตุผล"), ({"reason": "OTHER"}, "ระบุ"),
                         ({"reason": "NPO", "outcome": "MAYBE"}, "ผล")]:
        with pytest.raises(StoreError, match=message):
            cx.parse(bad)
    assert cx.suggest({"problems": ["labs", "npo"]}) == "PATIENT_UNFIT" and cx.suggest({"problems": []}) is None


def test_report_numbers():
    rows = [
        {"reason": "NPO", "outcome": "POSTPONED", "same_day": True, "surgeon": "Dr A", "procedure": "X", "case_code": "R-1",
         "planned": "2026-11-02", "note": None},
        {"reason": "BED", "outcome": "CANCELLED", "same_day": False, "surgeon": "Dr A", "procedure": "Y", "case_code": "R-2",
         "planned": "2026-11-03", "note": "ICU เต็ม"},
        {"reason": "EMERGENCY", "outcome": "POSTPONED", "same_day": True, "surgeon": "Dr B", "procedure": "Z",
         "case_code": "R-3", "planned": "2026-11-01", "note": None},
    ]
    s = cx.summarize(rows, done=17, waiting=2)
    assert (s["total"], s["scheduled"], s["rate"], s["same_day"], s["same_day_rate"]) == (3, 20, 15.0, 2, 10.0)
    assert (s["hospital"], s["patient"], s["hospital_pct"], s["postponed"], s["cancelled"]) == (2, 1, 66.7, 2, 1)
    assert s["waiting_given_up"] == 2 and s["by_surgeon"][0] == {"name": "Dr A", "count": 2}
    assert [r["case_code"] for r in s["recent"]] == ["R-2", "R-1", "R-3"]  # newest planned day first
    assert cx.summarize([], done=0)["rate"] is None


def test_team_hears_that_the_case_is_called_off():
    from orchestra_engine.results import Assignment

    a = Assignment("R-1", "OR-1", 480, 570, 600, ("SUR-1", "SCR-1"), (), (), None, None, None)
    change = SimpleNamespace(case_id="R-1", before=a, after=None)
    users = [SimpleNamespace(user_id="n", role="staff", staff_id="SCR-1")]
    event = {"type": "schedule", "cause": "cancel", "day": "2026-11-02", "changes": [change], "cases": {},
             "unscheduled": [], "new_case": "R-1", "actor_id": "x"}
    [m] = build_messages(event, users)
    assert m.lines == ["R-1: งดผ่าตัด (08:00-09:30 ห้อง OR-1)"]
    [m] = build_messages({**event, "cause": "replan"}, users)
    assert "รอจัดวันใหม่" in m.lines[0]


# ------------------------------------------------------------- demo store

def test_demo_store_cancel_and_postpone():
    s = ScheduleStore(workers=2, time_limit_s=10)
    events = []
    s.listener = events.append
    bookings = s.state()["schedule"]["bookings"]
    first, second = bookings[0]["case_id"], bookings[1]["case_id"]
    out = s.cancel_booking(first, {"reason": "NPO"}, actor=ADMIN)
    assert out["outcome"] == "POSTPONED" and first not in s.result.assignments and first in s.pending
    s.cancel_booking(second, {"reason": "BED", "outcome": "CANCELLED"}, actor=ADMIN)
    assert second not in s.pending
    assert [e["cause"] for e in events] == ["cancel", "cancel"]
    s.cancel_pending(first, data={"reason": "NOT_NEEDED"}, actor=ADMIN)
    report = s.report()["cancellations"]
    assert (report["total"], report["patient"], report["hospital"], report["waiting_given_up"]) == (2, 1, 1, 1)
    assert s.activity[-1].title.startswith(f"ยกเลิกคำขอที่รอจัด {first}")

    other = {"name": "Dr X", "role": "surgeon", "staff_id": "not-theirs"}
    with pytest.raises(Forbidden):
        s.cancel_booking(bookings[2]["case_id"], {"reason": "NPO"}, actor=other)
    s.record_actual(bookings[2]["case_id"], "08:05", None, actor=ADMIN)
    with pytest.raises(StoreError, match="เริ่มผ่าตัดแล้ว"):
        s.cancel_booking(bookings[2]["case_id"], {"reason": "NPO"}, actor=ADMIN)


# ------------------------------------------------------- Supabase store

def _book(store, day, surgeon, procedure, hn, at):
    assert store.slot_check(_request(store.state(day), surgeon, procedure, hn), at, commit=True, day=day)["booked"]
    return next(b["case_id"] for b in store.state(day)["schedule"]["bookings"] if b["patient_hn"] == hn)


@needs_db
def test_calling_off_a_booked_case(store, db_url):
    events = []
    store.listener = events.append
    day = _tomorrow(store)
    a = _book(store, day, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-X1", "09:00")
    b = _book(store, day, "Dr.Napat", "Laparoscopic appendectomy", "HN-X2", "11:00")
    out = store.cancel_booking(a, {"reason": "NPO", "note": "ดื่มนม"}, day=day, actor=ADMIN)
    assert out == {"case_id": a, "reason": "NPO", "outcome": "POSTPONED", "reason_kept": True}
    state = store.state(day)
    assert a not in [x["case_id"] for x in state["schedule"]["bookings"]]
    assert [p["code"] for p in state["pending"]] == [a] and state["pending"][0]["was_on"] == day
    store.cancel_booking(b, {"reason": "BED", "outcome": "CANCELLED"}, day=day, actor=ADMIN)
    assert _rows(db_url, "SELECT status FROM surgery_requests WHERE patient_hn = 'HN-X2'") == [("CANCELLED",)]
    assert [e["cause"] for e in events if e["type"] == "schedule"][-2:] == ["cancel", "cancel"]
    assert _rows(db_url, """SELECT case_code, reason, outcome, note, room_code IS NOT NULL, cancelled_by
                            FROM case_cancellations ORDER BY cancellation_id""") == [
        (a, "NPO", "POSTPONED", "ดื่มนม", True, "Admin A (admin)"), (b, "BED", "CANCELLED", None, True, "Admin A (admin)")]

    pending = store.state(day)["pending"][0]["id"]
    store.cancel_pending(pending, data={"reason": "DECLINED"}, actor=ADMIN)
    assert _rows(db_url, "SELECT reason, outcome, planned_start IS NULL FROM case_cancellations "
                         "ORDER BY cancellation_id DESC LIMIT 1") == [("DECLINED", "CANCELLED", True)]
    activity = [x["title"] for x in store.state(day)["activity"]]
    assert any(t.startswith(f"งดผ่าตัด {a}: ผู้ป่วยไม่ได้งดน้ำงดอาหาร") for t in activity)

    report = store.report(day, day)["cancellations"]
    assert (report["total"], report["patient"], report["hospital"], report["same_day"]) == (2, 1, 1, 0)
    assert report["rate"] == round(100 * 2 / (store.report(day, day)["cases"] + 2), 1)

    with pytest.raises(StoreError, match="ไม่พบเคส"):
        store.cancel_booking(a, {"reason": "NPO"}, day=day, actor=ADMIN)
    surgeon = {"name": "Dr", "role": "surgeon", "staff_id": "not-the-surgeon"}
    c = _book(store, day, "Dr.Napat", "Open inguinal hernia repair", "HN-X3", "13:00")
    with pytest.raises(Forbidden):
        store.cancel_booking(c, {"reason": "NPO"}, day=day, actor=surgeon)


@needs_db
def test_cases_pushed_off_their_day_are_recorded(store, db_url):
    from app.admin_store import Inventory

    day = _tomorrow(store)
    case = _book(store, day, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-R1", "09:00")
    admin = Inventory(store)
    napat = next(p["id"] for p in admin.staff()["staff"] if p["name"].startswith("Dr.Napat"))
    admin.add_shifts({"type": "LEAVE", "date_from": day, "date_to": day, "start": "00:00", "end": "00:00",
                      "staff_ids": [napat]})
    out = store.replan(day, actor=ADMIN)
    assert case in [u["case_id"] for u in out["unscheduled"]]
    assert _rows(db_url, "SELECT case_code, reason, outcome FROM case_cancellations") == [(case, "REPLAN", "POSTPONED")]
    report = store.report(day, day)["cancellations"]
    assert report["by_reason"][0]["reason"] == "REPLAN" and report["hospital"] == 1


@needs_db
def test_works_before_migration_010(db_url):
    import psycopg

    from app.db_store import DatabaseStore

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE case_cancellations")
    s = DatabaseStore(db_url, workers=2, time_limit_s=10)
    s.seed_demo()
    day = _tomorrow(s)
    case = _book(s, day, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-M1", "09:00")
    out = s.cancel_booking(case, {"reason": "NPO"}, day=day, actor=ADMIN)
    assert out["reason_kept"] is False and case not in [b["case_id"] for b in s.state(day)["schedule"]["bookings"]]
    report = s.report()
    assert report["cancellations"] is None and report["cancellations_ready"] is False
