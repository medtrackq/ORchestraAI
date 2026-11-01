"""OR Facility Agent: planned room closures and airborne-infection cases."""
from __future__ import annotations

from orchestra_engine import (
    NEGATIVE_PRESSURE,
    ORchestraOptimizer,
    OperatingRoom,
    StaffMember,
    StaffRole,
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


def solve(rooms, staff, cases):
    opt = ORchestraOptimizer(rooms, [], staff, time_limit_s=10, num_workers=4)
    result = opt.solve_schedule(cases, "facility", verbose=False)
    assert validate_schedule(result, cases, rooms, [], staff) == []
    return opt, result


def test_planned_closure_keeps_the_room_free():
    rooms = [OperatingRoom("OR-1", closures=[(hm("08:00"), hm("12:00"), "deep cleaning")])]
    _, result = solve(rooms, team("A"), [case("X", preferred_start=hm("08:00"))])
    assert result.assignments["X"].start_min == hm("12:00")
    assert any("เปิดอีกครั้งหลังปิดห้อง (deep cleaning)" in why for why in result.assignments["X"].reasoning)


def test_room_closed_all_day_is_reported_with_the_reason():
    rooms = [OperatingRoom("OR-1", closures=[(0, 24 * 60, "air handling repair")])]
    _, result = solve(rooms, team("A"), [case("X")])
    reason = result.unscheduled["X"][0]
    assert "ปิด" in reason and "air handling repair" in reason


def test_closure_moves_booked_cases_when_the_day_is_replanned():
    rooms = [OperatingRoom("OR-1"), OperatingRoom("OR-2")]
    staff = team("A") + team("B")
    cases = [case("A1", preferred_start=hm("09:00")), case("B1", "SUR-B", preferred_start=hm("09:00"))]
    opt, day = solve(rooms, staff, cases)
    busy_room = day.assignments["A1"].or_id
    closed = [OperatingRoom(r.or_id, closures=[(hm("08:00"), hm("13:00"), "repair")] if r.or_id == busy_room else [])
              for r in rooms]
    opt = ORchestraOptimizer(closed, [], staff, time_limit_s=10, num_workers=4)
    errors = validate_schedule(day, cases, closed, [], staff)
    assert any("ปิด 08:00-13:00 (repair)" in e for e in errors)
    after = opt.reschedule(day, cases, now_min=hm("07:00"), verbose=False)
    assert validate_schedule(after, cases, closed, [], staff) == []
    assert len(after.assignments) == 2 and after.changes


def test_case_running_when_the_room_closes_is_not_a_conflict():
    rooms = [OperatingRoom("OR-1")]
    opt, day = solve(rooms, team("A"), [case("RUN", minutes=120, preferred_start=hm("08:00")),
                                        case("NEXT", preferred_start=hm("11:00"))])
    closed = [OperatingRoom("OR-1", closures=[(hm("09:00"), hm("14:00"), "leak")])]
    opt = ORchestraOptimizer(closed, [], team("A"), time_limit_s=10, num_workers=4)
    after = opt.reschedule(day, list(day.cases.values()), now_min=hm("09:00"), verbose=False)
    assert after.is_feasible and after.assignments["RUN"].frozen
    assert after.assignments["NEXT"].start_min >= hm("14:00")


def test_airborne_case_needs_negative_pressure_and_long_cleaning():
    rooms = [OperatingRoom("OR-1"), OperatingRoom("OR-NP", NEGATIVE_PRESSURE, infectious_turnover_mins=90)]
    _, result = solve(rooms, team("A"), [case("TB", airborne=True, urgency=Urgency.URGENT)])
    a = result.assignments["TB"]
    assert a.or_id == "OR-NP" and a.cleanup_end_min - a.end_min == 90

    _, result = solve([OperatingRoom("OR-1")], team("A"), [case("TB", airborne=True)])
    assert "ไม่มีห้องที่มี [NEGATIVE_PRESSURE]" in result.unscheduled["TB"][0]


def test_slot_scan_names_the_closure():
    rooms = [OperatingRoom("OR-1", closures=[(hm("08:00"), hm("10:00"), "inspection")])]
    opt, day = solve(rooms, team("A"), [])
    check = opt.check_slot(day, case("X"), hm("08:30"), verbose=False)
    facility = next(v for v in check.verdicts if v.agent == "OR Facility Agent")
    assert not facility.ok and "ปิดห้อง 08:00-10:00 (inspection)" in facility.items[0].note
    assert check.assignment.start_min == hm("10:00")
