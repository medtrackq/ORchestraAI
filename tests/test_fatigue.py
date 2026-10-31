"""Staffing Agent fatigue rules: daily / 7-day hours, rest after night work,
emergency overrides with warnings, and spreading work to the least tired."""
from __future__ import annotations

from orchestra_engine import (
    Assignment,
    ORchestraOptimizer,
    OperatingRoom,
    StaffMember,
    StaffRole,
    SurgeryCase,
    Urgency,
    hm,
    staff_fatigue,
    validate_schedule,
)
from orchestra_engine.fatigue import is_late_end, late_windows

DAY = [(0, 24 * 60)]  # on duty all day (on-call)


def team(tag: str, shifts=DAY, **kw) -> list[StaffMember]:
    return [
        StaffMember(f"SUR-{tag}", f"Surgeon {tag}", StaffRole.SURGEON, shifts, **kw),
        StaffMember(f"ANE-{tag}", f"Anesth {tag}", StaffRole.ANESTHESIOLOGIST, shifts, **kw),
        StaffMember(f"SCR-{tag}", f"Scrub {tag}", StaffRole.SCRUB_NURSE, shifts, **kw),
        StaffMember(f"CIR-{tag}", f"Circ {tag}", StaffRole.CIRCULATING_NURSE, shifts, **kw),
    ]


def case(cid, surgeon, minutes, **kw) -> SurgeryCase:
    return SurgeryCase(cid, f"HN-{cid}", f"Procedure {cid}", surgeon, minutes, **kw)


def at(minute: int) -> dict:
    return {"release_min": minute, "deadline_min": minute}


def solve(staff, cases, rooms=None):
    rooms = rooms or [OperatingRoom("OR-1"), OperatingRoom("OR-2")]
    opt = ORchestraOptimizer(rooms, [], staff, time_limit_s=10, num_workers=4)
    result = opt.solve_schedule(cases, "fatigue", verbose=False)
    assert validate_schedule(result, cases, rooms, [], staff) == []
    return opt, result


def test_night_windows():
    assert late_windows(0, 24 * 60) == [(0, hm("06:00")), (hm("22:01"), 24 * 60)]
    assert late_windows(hm("08:00"), hm("20:00")) == []
    assert is_late_end(hm("23:00")) and is_late_end(hm("06:00")) and is_late_end(24 * 60)
    assert not is_late_end(hm("22:00")) and not is_late_end(hm("06:01"))


def test_daily_hour_limit_blocks_a_normal_case():
    staff = team("A", max_daily_hours=3)
    cases = [case("X1", "SUR-A", 120), case("X2", "SUR-A", 120)]
    _, result = solve(staff, cases)
    assert len(result.assignments) == 1
    assert len(result.unscheduled) == 1


def test_rest_after_a_night_case_delays_the_next_normal_case():
    staff = team("A")  # one team, rest 8 h
    cases = [
        case("NIGHT", "SUR-A", 120, urgency=Urgency.EMERGENCY, **at(hm("02:00"))),  # ends 04:00
        case("NEXT", "SUR-A", 60, release_min=hm("08:00")),
    ]
    _, result = solve(staff, cases)
    assert result.assignments["NEXT"].start_min >= hm("12:00")  # 04:00 + 8 h
    assert result.fatigue == []


def test_emergency_may_break_rest_and_is_reported():
    staff = team("A")
    cases = [
        case("NIGHT", "SUR-A", 120, urgency=Urgency.EMERGENCY, **at(hm("02:00"))),
        case("ER2", "SUR-A", 60, urgency=Urgency.EMERGENCY, release_min=hm("08:00")),
    ]
    _, result = solve(staff, cases)
    assert result.assignments["ER2"].start_min == hm("08:00")  # no waiting for a rested team
    rest = [i for i in result.fatigue if i.kind == "rest"]
    assert {i.staff_id for i in rest} == {"SUR-A", "ANE-A", "SCR-A", "CIR-A"}
    issue = rest[0]
    assert issue.override and not issue.is_error and issue.case_id == "ER2"
    assert issue.after_case == "NIGHT" and issue.minutes == hm("04:00")  # rest ends 12:00, starts 08:00
    assert "EMERGENCY override" in issue.detail()


def test_emergency_prefers_a_rested_person_when_there_is_one():
    staff = team("A") + [StaffMember("ANE-FRESH", "Fresh", StaffRole.ANESTHESIOLOGIST, DAY)]
    cases = [
        case("NIGHT", "SUR-A", 120, urgency=Urgency.EMERGENCY, **at(hm("02:00"))),
        case("ER2", "SUR-A", 60, urgency=Urgency.EMERGENCY, release_min=hm("08:00")),
    ]
    _, result = solve(staff, cases)
    night, er2 = result.assignments["NIGHT"].staff_ids, result.assignments["ER2"].staff_ids
    assert night[1] != er2[1]  # the anaesthetist of the night case gets to rest
    assert {i.staff_id for i in result.fatigue} == {"SUR-A", "SCR-A", "CIR-A"}  # nobody else could


def test_rest_from_the_previous_day():
    staff = team("A", rest_until=hm("09:00"))  # yesterday's late case
    _, result = solve(staff, [case("X", "SUR-A", 60, release_min=hm("07:00"))])
    assert result.assignments["X"].start_min >= hm("09:00")

    _, result = solve(staff, [case("X", "SUR-A", 60, **at(hm("07:00")))])
    assert "rests until 09:00" in result.unscheduled["X"][0]

    _, result = solve(staff, [case("ER", "SUR-A", 60, urgency=Urgency.EMERGENCY, **at(hm("07:00")))])
    assert result.assignments["ER"].start_min == hm("07:00")
    issue = next(i for i in result.fatigue if i.staff_id == "SUR-A")
    assert issue.kind == "rest" and issue.after_case is None and issue.override


def test_late_case_needs_rest_before_tomorrows_booking():
    tomorrow_7 = 24 * 60 + hm("07:00")
    staff = team("A", next_booking_min=tomorrow_7)
    ok = case("OK", "SUR-A", 120, **at(hm("21:00")))  # ends 23:00, 8 h rest until 07:00
    _, result = solve(staff, [ok])
    assert "OK" in result.assignments

    late = case("LATE", "SUR-A", 120, **at(hm("21:30")))  # ends 23:30: only 7.5 h rest
    _, result = solve(staff, [late])
    assert "LATE" in result.unscheduled

    er = case("ER", "SUR-A", 120, urgency=Urgency.EMERGENCY, **at(hm("21:30")))
    _, result = solve(staff, [er])
    issue = next(i for i in result.fatigue if i.staff_id == "SUR-A")
    assert issue.kind == "next_day" and issue.minutes == 30 and issue.override


def test_weekly_limit_override_for_emergency_only():
    staff = team("A", max_weekly_hours=48, current_weekly_hours=47)  # 60 min left
    _, result = solve(staff, [case("X", "SUR-A", 120)])
    assert "X" in result.unscheduled
    _, result = solve(staff, [case("ER", "SUR-A", 120, urgency=Urgency.EMERGENCY)])
    assert "ER" in result.assignments
    weekly = [i for i in result.fatigue if i.kind == "weekly"]
    assert len(weekly) == 4 and all(i.minutes == 60 and i.override for i in weekly)


def test_work_goes_to_the_least_tired_person():
    shift = [(hm("08:00"), hm("18:00"))]
    staff = [
        *team("A", shift)[:1], *team("A", shift)[2:],
        StaffMember("ANE-TIRED", "Tired", StaffRole.ANESTHESIOLOGIST, shift, current_weekly_hours=40),
        StaffMember("ANE-FRESH", "Fresh", StaffRole.ANESTHESIOLOGIST, shift, current_weekly_hours=8),
    ]
    _, result = solve(staff, [case("X", "SUR-A", 90)])
    assert "ANE-FRESH" in result.assignments["X"].staff_ids


def test_validator_reports_fatigue_breaks_of_normal_cases():
    staff = team("A", max_daily_hours=2)
    cases = [case("A1", "SUR-A", 90), case("A2", "SUR-A", 90)]
    people = ("SUR-A", "ANE-A", "SCR-A", "CIR-A")
    schedule = {
        "A1": Assignment("A1", "OR-1", hm("08:00"), hm("09:30"), hm("10:00"), people),
        "A2": Assignment("A2", "OR-1", hm("10:00"), hm("11:30"), hm("12:00"), people),
    }
    errors = validate_schedule(schedule, cases, [OperatingRoom("OR-1")], [], staff)
    assert sum("fatigue daily" in e for e in errors) == 4


def test_slot_check_reports_rest_and_offers_the_rested_slot():
    staff = team("A")
    night = case("NIGHT", "SUR-A", 120, urgency=Urgency.EMERGENCY, **at(hm("02:00")))
    opt, day = solve(staff, [night])
    check = opt.check_slot(day, case("NEW", "SUR-A", 60), hm("09:00"), verbose=False)
    staffing = next(v for v in check.verdicts if v.agent == "Staffing Agent")
    assert not staffing.ok
    assert "resting until 12:00 after late case NIGHT" in staffing.items[0].note
    assert check.assignment.start_min == hm("12:00")


def test_staff_profile_scores_load():
    staff = team("A", max_daily_hours=10, current_weekly_hours=24)
    _, result = solve(staff, [case("X", "SUR-A", 300)])
    profile = {p["staff_id"]: p for p in staff_fatigue(staff, result.assignments, result.fatigue)}
    sur = profile["SUR-A"]
    assert sur["today_mins"] == 300 and sur["week_mins"] == 24 * 60 + 300
    assert sur["score"] == 60 and sur["level"] == "ok"  # 29 h of 48 h = 60 %, 5 h of 10 h = 50 %
