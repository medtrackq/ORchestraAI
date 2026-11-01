"""Weekly OR blocks: the engine's per-case room limits, the block rules, the admin data and the report.

Engine and rule tests need no database; the Supabase ones run against a real
PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import blocks as bk
from app.store import StoreError
from orchestra_engine import ORchestraOptimizer, OperatingRoom, StaffMember, StaffRole, SurgeryCase, hm, validate_schedule
from orchestra_engine.agents import FACILITY
from test_db import ADMIN_URL, _request, db_url, store  # noqa: F401  (fixtures)

ADMIN = {"name": "Admin A", "username": "a", "role": "admin", "staff_id": None, "user_id": "u-admin"}
TZ = ZoneInfo("Asia/Bangkok")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
ORTHO_BLOCK = (hm("08:00"), hm("16:00"), "บล็อกของ Orthopedics")


# ------------------------------------------------------------------ engine

def _world():
    shift = [(hm("08:00"), hm("18:00"))]
    staff = [StaffMember("SUR-A", "Surgeon A", StaffRole.SURGEON, shift),
             StaffMember("ANE-1", "Anesth 1", StaffRole.ANESTHESIOLOGIST, shift),
             StaffMember("SCR-1", "Scrub 1", StaffRole.SCRUB_NURSE, shift),
             StaffMember("CIR-1", "Circ 1", StaffRole.CIRCULATING_NURSE, shift)]
    rooms = [OperatingRoom("OR-1"), OperatingRoom("OR-2")]
    opt = ORchestraOptimizer(rooms, [], staff, [], time_limit_s=10)
    return opt, rooms, staff, opt.solve_schedule([], "empty", verbose=False)


def test_a_case_keeps_out_of_another_owners_block():
    opt, rooms, staff, empty = _world()
    case = SurgeryCase("N1", "HN-1", "Hernia", "SUR-A", 60, room_blocks={"OR-1": [ORTHO_BLOCK]})
    check = opt.check_slot(empty, case, hm("09:00"), verbose=False)
    assert check.assignment.or_id == "OR-2" and check.assignment.start_min == hm("09:00")
    facility = next(v for v in check.verdicts if v.agent == FACILITY)
    assert any(i.resource == "OR-1" and not i.ok and "บล็อกของ Orthopedics" in i.note for i in facility.items)
    assert validate_schedule(check.result, check.result.cases.values(), rooms, [], staff, []) == []

    both = SurgeryCase("N2", "HN-2", "Hernia", "SUR-A", 60, room_blocks={"OR-1": [ORTHO_BLOCK], "OR-2": [ORTHO_BLOCK]})
    late = opt.check_slot(empty, both, hm("09:00"), verbose=False)
    assert late.assignment.start_min >= hm("16:00") - 0  # after the blocks (the room is still open)
    assert late.assignment.start_min + 60 <= hm("18:00")

    # rooms that take elective cases only 08:00-16:00: the blocks leave nothing, and the reason says so
    day_rooms = [OperatingRoom(r, elective_start=hm("08:00"), elective_end=hm("16:00")) for r in ("OR-1", "OR-2")]
    day_opt = ORchestraOptimizer(day_rooms, [], staff, [], time_limit_s=10)
    plan, reasons = day_opt._plan_case(SurgeryCase("N3", "HN-3", "Long", "SUR-A", 300,
                                                   room_blocks={"OR-1": [ORTHO_BLOCK], "OR-2": [ORTHO_BLOCK]}), hm("09:00"))
    assert plan is None and any("บล็อกห้องของคนอื่น" in r and "บล็อกของ Orthopedics" in r for r in reasons)


def test_the_owners_block_room_is_preferred():
    opt, _, _, empty = _world()
    for room in ("OR-1", "OR-2"):
        case = SurgeryCase(f"P-{room}", "HN-P", "Knee", "SUR-A", 60, preferred_rooms=(room,))
        assert opt.check_slot(empty, case, hm("09:00"), verbose=False).assignment.or_id == room


# ------------------------------------------------------------------- rules

def _block(**kw):
    base = dict(id="b1", room="OR-1", start_min=480, end_min=960, owner_kind="SPECIALTY", specialty="Orthopedics",
                surgeon_id=None, owner="Orthopedics", release_days=2)
    return bk.DayBlock(**{**base, **kw})


def test_block_rules():
    day, today = date(2026, 11, 9), date(2026, 11, 2)
    assert bk.release_state("b1", 2, day, today, set()) == (False, None)
    assert bk.release_state("b1", 2, day, day - timedelta(days=2), set())[0]  # two days before: open to all
    assert bk.release_state("b1", 2, day, today, {"b1"}) == (True, "เจ้าของคืนบล็อกวันนี้แล้ว")

    ortho, mine = _block(), _block(id="b2", room="OR-2", owner_kind="SURGEON", specialty=None, surgeon_id="S-9", owner="Dr K")
    held, own = bk.case_limits([ortho, mine], "ELECTIVE", "S-1", "General Surgery")
    assert set(held) == {"OR-1", "OR-2"} and own == ()
    assert held["OR-1"][0][:2] == (480, 960) and "Orthopedics" in held["OR-1"][0][2]
    held, own = bk.case_limits([ortho, mine], "ELECTIVE", "S-1", " orthopedics ")  # specialty matches loosely
    assert set(held) == {"OR-2"} and own == ("OR-1",)
    assert bk.case_limits([ortho, mine], "ELECTIVE", "S-9", None) == (
        {"OR-1": [(480, 960, "บล็อกของ Orthopedics (เปิดให้คนอื่น 2 วันก่อนผ่าตัด)")]}, ("OR-2",))  # Dr K's own block
    assert bk.case_limits([ortho], "URGENT", "S-1", None) == ({}, ())  # urgent / emergency cases ignore blocks
    assert bk.case_limits([_block(released=True)], "ELECTIVE", "S-1", None) == ({}, ())


def test_block_usage_numbers():
    rows = [{"block_id": "b1", "room": "OR-1", "weekday": 0, "start_time": time(8), "end_time": time(12),
             "owner_kind": "SPECIALTY", "specialty": "Ortho", "surgeon_id": None, "owner": "Ortho", "release_days": 2,
             "valid_from": None, "valid_to": date(2026, 11, 9)}]
    occ = bk.occurrences(rows, date(2026, 11, 2), date(2026, 11, 16))  # Mondays 2, 9 (16 is after valid_to)
    assert [o["day"] for o in occ] == [date(2026, 11, 2), date(2026, 11, 9)]
    occ[1]["given_back"] = True
    at = lambda d, h, m=0: datetime(2026, 11, d, h, m, tzinfo=TZ)  # noqa: E731
    cases = [{"room": "OR-1", "start": at(2, 8), "end": at(2, 11), "surgeon_id": "x", "specialty": "Ortho"},
             {"room": "OR-1", "start": at(9, 9), "end": at(9, 10), "surgeon_id": "y", "specialty": "Gen"},
             {"room": "OR-2", "start": at(2, 8), "end": at(2, 12), "surgeon_id": "x", "specialty": "Ortho"}]
    [row] = bk.usage(occ, cases, TZ)
    assert row == {"owner": "Ortho", "kind": "SPECIALTY", "blocks": 2, "hours": 8.0, "own_hours": 3.0, "others_hours": 1.0,
                   "utilization": 37.5, "given_back": 1}


# ------------------------------------------------------- Supabase store

@pytest.fixture()
def admin(store):
    from app.admin_store import Inventory

    return Inventory(store)


def _next(weekday: int, store, at_least: int = 5) -> date:
    day = store.db.today() + timedelta(days=at_least)
    return day + timedelta(days=(weekday - day.weekday()) % 7)


@needs_db
def test_blocks_are_kept_and_checked(admin):
    made = admin.save_block({"room": "OR-1", "weekdays": [0, 2], "start": "08:00", "end": "12:00",
                             "owner_kind": "SPECIALTY", "specialty": "Orthopedics", "release_days": 3}, actor=ADMIN)
    assert len(made["ids"]) == 2
    data = admin.blocks()
    assert data["ready"] and "Orthopedics" in data["specialties"]
    assert sorted((b["room"], b["weekday"], b["start"], b["owner"]) for b in data["blocks"]) == [
        ("OR-1", 0, "08:00", "Orthopedics"), ("OR-1", 2, "08:00", "Orthopedics")]
    kittipong = next(s["id"] for s in data["surgeons"] if s["name"].startswith("Dr.Kittipong"))
    for bad, message in [({"start": "12:00", "end": "08:00"}, "หลังเวลาเริ่ม"), ({"weekdays": []}, "วันในสัปดาห์"),
                         ({"owner_kind": "SPECIALTY", "specialty": " "}, "แผนก"), ({"room": "OR-9"}, "ไม่พบห้อง"),
                         ({"start": "11:00", "end": "13:00"}, "เป็นบล็อกของ Orthopedics อยู่แล้ว"),
                         ({"valid_from": "2027-01-10", "valid_to": "2027-01-01"}, "สิ้นสุด")]:
        with pytest.raises(StoreError, match=message):
            admin.save_block({"room": "OR-1", "weekdays": [0], "start": "13:00", "end": "16:00", "owner_kind": "SURGEON",
                              "surgeon_id": kittipong, **bad})
    later = admin.save_block({"room": "OR-1", "weekdays": [0], "start": "13:00", "end": "16:00", "owner_kind": "SURGEON",
                              "surgeon_id": kittipong, "release_days": 1}, actor=ADMIN)["ids"][0]
    admin.save_block({"room": "OR-1", "weekdays": [1], "start": "13:00", "end": "17:00", "owner_kind": "SURGEON",
                      "surgeon_id": kittipong, "release_days": 1}, block_id=later)
    assert next(b for b in admin.blocks()["blocks"] if b["id"] == later)["weekday"] == 1
    admin.delete_block(later)
    assert len(admin.blocks()["blocks"]) == 2

    monday = _next(0, admin.store).isoformat()
    on_day = admin.blocks(monday)["on_day"]
    assert [(b["room"], b["released"]) for b in on_day["blocks"]] == [("OR-1", False)]
    admin.release_block(on_day["blocks"][0]["id"], monday, actor=ADMIN)
    assert admin.blocks(monday)["on_day"]["blocks"][0]["released_why"] == "เจ้าของคืนบล็อกวันนี้แล้ว"
    with pytest.raises(StoreError, match="ไม่ได้อยู่ในวันที่"):
        admin.release_block(on_day["blocks"][0]["id"], (date.fromisoformat(monday) + timedelta(days=1)).isoformat())
    admin.release_block(on_day["blocks"][0]["id"], monday, undo=True)
    assert admin.blocks(monday)["on_day"]["blocks"][0]["released"] is False


@needs_db
def test_booking_respects_blocks(admin, store):
    for room in ("OR-1", "OR-2", "OR-3", "OR-4"):
        admin.save_block({"room": room, "weekdays": [2], "start": "08:00", "end": "16:00", "owner_kind": "SPECIALTY",
                          "specialty": "Orthopedics", "release_days": 2}, actor=ADMIN)
    day = _next(2, store).isoformat()
    state = store.state(day)
    assert [b["owner"] for b in state["blocks"]] == ["Orthopedics"] * 4 and not any(b["released"] for b in state["blocks"])

    def placed(data, at="09:00"):
        out = store.slot_check(data, at, commit=False, day=day)
        return next(((b["or_id"], b["start"]) for b in out["result"]["bookings"] if b["patient_hn"] == data["patient_hn"]),
                    None), out
    general = _request(state, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-K1")
    where, out = placed(general)
    assert where is None  # Dr.Napat (General Surgery) cannot use Orthopedics' block that day
    facility = next(v for v in out["verdicts"] if v["agent"] == FACILITY)
    assert all("บล็อกของ Orthopedics" in i["note"] for i in facility["items"] if not i["ok"])
    assert placed(_request(state, "Dr.Napat", "Laparoscopic cholecystectomy", "HN-K2", urgency="URGENT"))[0] is not None
    assert placed(_request(state, "Dr.Somchai", "Arthroscopic ACL reconstruction", "HN-K3"))[0] == ("OR-1", "09:00")

    block = next(b for b in state["blocks"] if b["room"] == "OR-1")
    admin.release_block(block["id"], day)
    assert placed(general)[0] == ("OR-1", "09:00")  # given back: open to everyone
    assert store.slot_check(general, "09:00", commit=True, day=day)["booked"]

    report = store.report(day, day)
    ortho = next(r for r in report["blocks"] if r["owner"] == "Orthopedics")
    assert (ortho["blocks"], ortho["hours"], ortho["given_back"]) == (4, 32.0, 1)
    assert ortho["own_hours"] == 0 and ortho["others_hours"] > 0  # Dr.Napat's case sits in the given-back block


@needs_db
def test_works_before_migration_012(db_url):
    import psycopg

    from app.admin_store import Inventory
    from app.db_store import DatabaseStore

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE or_block_releases")
        conn.execute("DROP TABLE or_blocks")
    s = DatabaseStore(db_url, workers=2, time_limit_s=10)
    s.seed_demo()
    state = s.state()
    assert state["blocks"] == [] and state["blocks_ready"] is False
    a = Inventory(s)
    assert a.blocks()["ready"] is False
    with pytest.raises(StoreError, match="20261111000000_or_blocks.sql"):
        a.save_block({"room": "OR-1", "weekdays": [0], "start": "08:00", "end": "12:00", "owner_kind": "SPECIALTY",
                      "specialty": "Ortho"})
    assert s.report()["blocks_ready"] is False
