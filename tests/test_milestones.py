"""Steps of a case on the day (holding, in/out of the OR, incision, closure, recovery) and the status board.

Rule tests need no database; store tests run on the demo store, and the Supabase
ones against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

import pytest

from app import milestones as ms
from app.store import Forbidden, ScheduleStore, StoreError
from test_db import ADMIN_URL, _request, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

NURSE = {"name": "Nurse A", "username": "n", "role": "staff", "staff_id": "SCR-9", "user_id": "u-n"}
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
ACTUAL = ("in_room", "out_room")  # the case's actual times: its own team records them


def _team(booking) -> dict:
    """A nurse on the case's team (NURSE, a holding / recovery nurse, is not)."""
    member = next(t["staff_id"] for t in booking["team"][1:])
    return {"name": "Circ B", "username": "c", "role": "staff", "staff_id": member, "user_id": "u-c"}


# ------------------------------------------------------------------ rules

def test_stage_of_a_case():
    s = ms.summarize({}, 480, 470)
    assert (s["stage"], s["phase"], s["next"], s["since"]) == ("scheduled", "scheduled", "arrived", None)
    late = ms.summarize({}, 480, 500)
    assert (late["stage"], late["phase"], late["late_min"]) == ("late", "late", 20)
    assert ms.summarize({}, 480, 500, late=False)["stage"] == "scheduled"
    live = ms.summarize({"arrived": 450, "in_room": 475, "incision": 490}, 480, 520)
    assert (live["stage"], live["label"], live["phase"], live["since"], live["elapsed"], live["next"]) == (
        "incision", "กำลังผ่าตัด", "in_or", "08:10", 30, "closure")
    assert live["times"]["in_room"] == "07:55" and live["times"]["closure"] is None
    done = ms.summarize({"in_room": 480, "out_room": 560, "recovery_out": 640}, 480, 700)
    assert (done["phase"], done["elapsed"], done["next"]) == ("done", None, None)


def test_steps_stay_in_order():
    times = {"in_room": 480, "incision": 500}
    ms.check(times, "closure", 600)
    for step, minute, message in [("closure", 490, "ต้องไม่ก่อน ลงมีด"), ("arrived", 485, "ต้องไม่หลัง เข้าห้องผ่าตัด"),
                                  ("out_room", 470, "ต้องไม่ก่อน")]:
        with pytest.raises(StoreError, match=message):
            ms.check(times, step, minute)
    with pytest.raises(StoreError, match="เข้าห้องผ่าตัดก่อน"):
        ms.check({}, "out_room", 600)
    ms.check(times, "incision", None, clear=True)
    with pytest.raises(StoreError, match="ลบได้เฉพาะขั้นล่าสุด"):
        ms.check(times, "in_room", None, clear=True)
    assert ms.place({"in_room": 23 * 60}, "out_room", 30) == 24 * 60 + 30  # out after midnight
    assert ms.place({"in_room": 8 * 60}, "out_room", 7 * 60) == 7 * 60  # earlier the same morning: an error later
    with pytest.raises(StoreError):
        ms.parse_update({"step": "coffee"})


def test_time_per_step_for_the_report():
    cases = [{"arrived": 0, "in_room": 20, "incision": 50, "closure": 110, "out_room": 125, "recovery_out": 185},
             {"in_room": 0, "incision": 40, "closure": 80, "out_room": 100}, {"in_room": 0}]
    rows = {(r["from"], r["to"]): r for r in ms.segment_stats(cases)}
    assert rows[("in_room", "incision")] == {"from": "in_room", "to": "incision", "label": rows[("in_room", "incision")]["label"],
                                             "n": 2, "mean": 35.0, "median": 35.0}
    assert rows[("in_room", "out_room")]["mean"] == 102.5 and rows[("out_room", "recovery_out")]["n"] == 1


# ------------------------------------------------------------- demo store

def test_demo_store_steps():
    s = ScheduleStore(workers=2, time_limit_s=10)
    first = s.state()["schedule"]["bookings"][0]
    case, team = first["case_id"], _team(first)
    with pytest.raises(Forbidden, match="ทีม"):  # not on this case: may not set its actual start
        s.milestone_update(case, {"step": "in_room", "time": "07:50"}, actor=NURSE)
    for step, at in [("arrived", "07:30"), ("in_room", "07:50"), ("incision", "08:20")]:
        out = s.milestone_update(case, {"step": step, "time": at}, actor=team if step in ACTUAL else NURSE)
    assert out["milestones"]["stage"] == "incision"
    booking = next(b for b in s.state()["schedule"]["bookings"] if b["case_id"] == case)
    assert booking["actual_start"] == "07:50"  # into the OR is the actual start the reports use
    with pytest.raises(StoreError, match="ต้องไม่ก่อน"):
        s.milestone_update(case, {"step": "closure", "time": "08:00"}, actor=NURSE)
    s.milestone_update(case, {"step": "closure", "time": "09:30"}, actor=NURSE)
    s.milestone_update(case, {"step": "out_room", "time": "09:45"}, actor=team)
    assert s.activity[-1].title.startswith(f"{case} ออกจากห้องผ่าตัด: ใช้ห้อง 115 นาที")
    with pytest.raises(Forbidden):
        s.milestone_update(case, {"step": "out_room", "clear": True}, actor=NURSE)
    s.milestone_update(case, {"step": "out_room", "clear": True}, actor=team)
    assert s.state()["milestones"][case]["stage"] == "closure"
    stages = {r["to"]: r for r in s.report()["stages"]}
    assert stages["incision"]["mean"] == 30 and stages["closure"]["mean"] == 70
    other = {"name": "Dr X", "role": "surgeon", "staff_id": "not-theirs"}
    with pytest.raises(Forbidden):
        s.milestone_update(case, {"step": "out_room", "time": "09:45"}, actor=other)
    if s._clock() < 23 * 60 + 50:  # a time later than now is refused
        with pytest.raises(StoreError, match="ล่วงหน้า"):
            s.milestone_update(case, {"step": "out_room", "time": "23:59"}, actor=team)


# ------------------------------------------------------- Supabase store

@needs_db
def test_steps_are_saved_with_the_case(store, db_url):
    day = _tomorrow(store)
    assert store.slot_check(_request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-M1"), "09:00",
                            commit=True, day=day)["booked"]
    case = next(b["case_id"] for b in store.state(day)["schedule"]["bookings"] if b["patient_hn"] == "HN-M1")
    with pytest.raises(StoreError, match="ต้องใส่เวลา"):  # not today: say when
        store.milestone_update(case, {"step": "arrived"}, day=day, actor=NURSE)
    team = _team(next(b for b in store.state(day)["schedule"]["bookings"] if b["case_id"] == case))
    for step, at in [("arrived", "08:30"), ("in_room", "08:55"), ("incision", "09:20"), ("closure", "10:30"),
                     ("out_room", "10:45"), ("recovery_out", "11:50")]:
        store.milestone_update(case, {"step": step, "time": at}, day=day, actor=team if step in ACTUAL else NURSE)
    state = store.state(day)
    m = state["milestones"][case]
    assert (m["stage"], m["phase"], m["times"]["incision"]) == ("recovery_out", "done", "09:20")
    booking = next(b for b in state["schedule"]["bookings"] if b["case_id"] == case)
    assert (booking["actual_start"], booking["actual_end"]) == ("08:55", "10:45")
    assert sorted(r[0] for r in _rows(db_url, "SELECT step FROM case_milestones")) == [
        "arrived", "closure", "incision", "recovery_out"]
    assert _rows(db_url, "SELECT DISTINCT recorded_by FROM case_milestones") == [("Nurse A (staff)",)]
    stages = {r["to"]: r for r in store.report(day, day)["stages"]}
    assert (stages["incision"]["mean"], stages["closure"]["mean"], stages["recovery_out"]["mean"]) == (25, 70, 65)
    store.milestone_update(case, {"step": "recovery_out", "clear": True}, day=day, actor=NURSE)
    assert store.state(day)["milestones"][case]["stage"] == "out_room"
    surgeon = {"name": "Dr", "role": "surgeon", "staff_id": "not-the-surgeon"}
    with pytest.raises(Forbidden):
        store.milestone_update(case, {"step": "recovery_out", "time": "12:00"}, day=day, actor=surgeon)


@needs_db
def test_works_before_migration_011(db_url):
    import psycopg

    from app.db_store import DatabaseStore

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE case_milestones")
    s = DatabaseStore(db_url, workers=2, time_limit_s=10)
    s.seed_demo()
    day = _tomorrow(s)
    assert s.slot_check(_request(s.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-M2"), "09:00",
                        commit=True, day=day)["booked"]
    case = next(b["case_id"] for b in s.state(day)["schedule"]["bookings"] if b["patient_hn"] == "HN-M2")
    state = s.state(day)
    assert state["milestones_ready"] is False and state["milestones"][case]["stage"] == "scheduled"
    team = _team(next(b for b in s.state(day)["schedule"]["bookings"] if b["case_id"] == case))
    s.milestone_update(case, {"step": "in_room", "time": "09:00"}, day=day, actor=team)  # actual start still works
    with pytest.raises(StoreError, match="migration"):
        s.milestone_update(case, {"step": "incision", "time": "09:10"}, day=day, actor=NURSE)
    assert s.report(day, day)["stages_ready"] is False
