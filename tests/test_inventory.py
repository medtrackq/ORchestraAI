"""Equipment & Inventory Agent: single-use supplies and implants."""
from __future__ import annotations

from datetime import date, timedelta

from orchestra_engine import (
    ORchestraOptimizer,
    OperatingRoom,
    StaffMember,
    StaffRole,
    SupplyItem,
    SurgeryCase,
    Urgency,
    hm,
    validate_schedule,
)

SHIFT = [(hm("07:00"), hm("20:00"))]


def team(tag: str) -> list[StaffMember]:
    return [StaffMember(f"{role[:3]}-{tag}", f"{role} {tag}", role, SHIFT) for role in (
        StaffRole.SURGEON, StaffRole.ANESTHESIOLOGIST, StaffRole.SCRUB_NURSE, StaffRole.CIRCULATING_NURSE)]


def case(cid, surgeon="SUR-A", minutes=60, **kw) -> SurgeryCase:
    return SurgeryCase(cid, f"HN-{cid}", f"Procedure {cid}", surgeon, minutes, **kw)


ROOMS = [OperatingRoom("OR-1"), OperatingRoom("OR-2")]
STAFF = team("A") + team("B")


def solve(cases, supplies):
    opt = ORchestraOptimizer(ROOMS, [], STAFF, supplies=supplies, time_limit_s=10, num_workers=4)
    result = opt.solve_schedule(cases, "inventory", verbose=False)
    assert validate_schedule(result, cases, ROOMS, [], STAFF, supplies=supplies) == []
    return opt, result


def test_implant_in_stock_for_only_one_case():
    stock = [SupplyItem("KNEE", "Knee implant", 1, "ชุด", "IMPLANT", "next delivery 2026-11-05 (+4 ชุด)")]
    cases = [case("K1", required_supplies={"KNEE": 1}), case("K2", "SUR-B", required_supplies={"KNEE": 1})]
    _, result = solve(cases, stock)
    assert len(result.assignments) == 1
    [why] = result.unscheduled.values()
    assert "Knee implant (KNEE) runs out: 1 of 1" in why[0] and "next delivery 2026-11-05" in why[0]


def test_out_of_stock_and_unknown_items_are_reported():
    stock = [SupplyItem("MESH", "Hernia mesh", 0, note="no delivery on order")]
    _, result = solve([case("H1", required_supplies={"MESH": 1}), case("X1", "SUR-B", required_supplies={"NOPE": 1})], stock)
    assert result.unscheduled["H1"] == ["Inventory: need 1 x Hernia mesh (MESH), only 0 available; no delivery on order"]
    assert result.unscheduled["X1"] == ["Inventory: NOPE is not a stocked item"]


def test_emergency_takes_the_last_implant():
    stock = [SupplyItem("GRAFT", "Vascular graft", 1)]
    elective = case("EL", required_supplies={"GRAFT": 1}, preferred_start=hm("13:00"))
    opt, day = solve([elective], stock)
    er = case("ER", "SUR-B", urgency=Urgency.EMERGENCY, release_min=hm("09:00"), required_supplies={"GRAFT": 1})
    after = opt.insert_emergency(day, [elective], [er], now_min=hm("09:00"), verbose=False)
    assert "ER" in after.assignments and "EL" not in after.assignments
    assert "runs out" in after.unscheduled["EL"][0]
    assert validate_schedule(after, [elective, er], ROOMS, [], STAFF, supplies=stock) == []


def test_validator_counts_stock():
    stock = [SupplyItem("CEM", "Bone cement", 3)]
    cases = [case("A", required_supplies={"CEM": 2}), case("B", "SUR-B", required_supplies={"CEM": 2})]
    _, ok = solve(cases[:1], stock)
    both = dict(ok.assignments)
    _, other = solve(cases[1:], stock)
    both.update(other.assignments)
    errors = validate_schedule(both, cases, ROOMS, [], STAFF, supplies=stock)
    assert any("Bone cement (CEM): 4 booked, only 3 available" in e for e in errors)


def test_slot_scan_reports_stock():
    stock = [SupplyItem("DRAPE", "Robotic drape kit", 1, "ชุด")]
    opt, day = solve([case("R1", required_supplies={"DRAPE": 1})], stock)
    check = opt.check_slot(day, case("R2", "SUR-B", required_supplies={"DRAPE": 1}), hm("15:00"), verbose=False)
    equipment = next(v for v in check.verdicts if v.agent == "Equipment & CSSD Agent")
    assert not equipment.ok and "need 1, only 0 ชุด left" in equipment.items[-1].note
    assert check.assignment is None


def test_stock_for_a_day_keeps_later_bookings_and_counts_deliveries():
    from app.db import supply_available

    d = date(2026, 11, 2)
    later = [(d + timedelta(days=3), 1)]
    assert supply_available(1, [], later, d) == 0  # the case on day 3 keeps the only one
    assert supply_available(1, [(d + timedelta(days=1), 2)], later, d) == 1  # delivery before it arrives first
    assert supply_available(1, [(d + timedelta(days=5), 2)], later, d) == 0  # delivery too late for day 3
    assert supply_available(1, [(d + timedelta(days=5), 2)], later, d + timedelta(days=6)) == 2
    assert supply_available(2, [], [(d - timedelta(days=1), 1)], d) == 1  # yesterday's case not settled yet


def test_booked_cases_keep_their_stock_and_new_ones_cannot_take_later_bookings():
    # One implant on the shelf: today's booked case has it, a case booked for a later day needs
    # one more that has not arrived (so nothing is free for new cases today).
    stock = [SupplyItem("KNEE", "Knee implant", available=0, for_booked=1)]
    booked = case("OLD", required_supplies={"KNEE": 1}, preferred_start=hm("09:00"))
    opt = ORchestraOptimizer(ROOMS, [], STAFF, supplies=[SupplyItem("KNEE", "Knee implant", 1)],
                             time_limit_s=10, num_workers=4)
    day = opt.solve_schedule([booked], "booked earlier", verbose=False)
    opt = ORchestraOptimizer(ROOMS, [], STAFF, supplies=stock, time_limit_s=10, num_workers=4)
    assert validate_schedule(day, [booked], ROOMS, [], STAFF, supplies=stock) == []  # earlier case is served first

    replanned = opt.reschedule(day, [booked], now_min=hm("07:00"), verbose=False)
    assert "OLD" in replanned.assignments  # re-planning keeps it
    check = opt.check_slot(day, case("NEW", "SUR-B", required_supplies={"KNEE": 1}), hm("13:00"), verbose=False)
    assert check.assignment is None  # a new case may not take the later booking's implant
    er = case("ER", "SUR-B", urgency=Urgency.EMERGENCY, release_min=hm("08:00"), required_supplies={"KNEE": 1})
    after = opt.insert_emergency(day, [booked], [er], now_min=hm("08:00"), verbose=False)
    assert "ER" in after.assignments and "OLD" not in after.assignments  # the emergency gets the shelf implant
