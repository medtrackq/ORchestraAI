from __future__ import annotations

import pytest

from orchestra_engine import (
    Assignment,
    EquipmentUnit,
    InfectionRisk,
    ORchestraOptimizer,
    OperatingRoom,
    RecoveryBed,
    StaffMember,
    StaffRole,
    SurgeryCase,
    Urgency,
    hm,
    validate_schedule,
)
from orchestra_engine.demo import build_hospital, run_all


def crew(tag: str, start: str = "08:00", end: str = "18:00", **surgeon_kw) -> list[StaffMember]:
    shift = [(hm(start), hm(end))]
    return [
        StaffMember(f"SUR-{tag}", f"Surgeon {tag}", StaffRole.SURGEON, shift, **surgeon_kw),
        StaffMember(f"ANE-{tag}", f"Anesth {tag}", StaffRole.ANESTHESIOLOGIST, shift),
        StaffMember(f"SCR-{tag}", f"Scrub {tag}", StaffRole.SCRUB_NURSE, shift),
        StaffMember(f"CIR-{tag}", f"Circ {tag}", StaffRole.CIRCULATING_NURSE, shift),
    ]


def case(cid: str, surgeon: str, minutes: int, **kw) -> SurgeryCase:
    return SurgeryCase(cid, f"HN-{cid}", f"Procedure {cid}", surgeon, minutes, **kw)


def solve(rooms, equipments, staff, cases, beds=(), **kw):
    opt = ORchestraOptimizer(rooms, equipments, staff, beds, time_limit_s=10, **kw)
    result = opt.solve_schedule(cases, "test", verbose=False)
    assert validate_schedule(result, cases, rooms, equipments, staff, beds) == []
    return opt, result


def test_demo_scenarios_are_conflict_free():
    results = run_all(verbose=False)
    hospital = build_hospital()
    for key, result in results.items():
        assert result.is_feasible, key
        if key != "cssd":  # the CSSD scenario runs on a modified hospital
            assert hospital.validate(result) == [], key
    assert len(results["day"].assignments) == 10
    assert "E01" in results["emergency"].assignments
    assert results["overrun"].assignments["C04"].frozen


def test_room_turnover_is_longer_after_dirty_case():
    rooms = [OperatingRoom("OR-1", base_turnover_mins=30, infectious_turnover_mins=60)]
    staff = crew("A")
    cases = [
        case("DIRTY", "SUR-A", 60, infection_risk=InfectionRisk.DIRTY, preferred_start=hm("08:00")),
        case("CLEAN", "SUR-A", 60),
    ]
    _, result = solve(rooms, [], staff, cases)
    dirty, clean = result.assignments["DIRTY"], result.assignments["CLEAN"]
    assert dirty.cleanup_end_min == dirty.end_min + 60
    assert clean.start_min >= dirty.end_min + 60


def test_equipment_waits_for_cssd_sterilization_between_uses():
    rooms = [OperatingRoom("OR-1"), OperatingRoom("OR-2")]
    units = [EquipmentUnit("KNEE-01", "KNEE_SET", sterilization_mins=120)]
    staff = crew("A") + crew("B")
    cases = [
        case("K1", "SUR-A", 90, required_equipment_types=["KNEE_SET"], preferred_start=hm("08:00")),
        case("K2", "SUR-B", 90, required_equipment_types=["KNEE_SET"], preferred_start=hm("08:00")),
    ]
    _, result = solve(rooms, units, staff, cases)
    first, second = sorted(result.assignments.values(), key=lambda a: a.start_min)
    assert second.start_min >= first.end_min + 120
    assert any("back from CSSD" in why for why in second.reasoning)


def test_equipment_still_in_cssd_delays_start():
    rooms = [OperatingRoom("OR-1")]
    units = [EquipmentUnit("C-ARM", "C_ARM", status="STERILIZING", available_at=hm("09:30"))]
    staff = crew("A")
    cases = [case("X", "SUR-A", 60, required_equipment_types=["C_ARM"], preferred_start=hm("08:00"))]
    _, result = solve(rooms, units, staff, cases)
    assert result.assignments["X"].start_min == hm("09:30")


def test_room_capabilities_and_room_type_are_matched():
    rooms = [
        OperatingRoom("OR-STD"),
        OperatingRoom("OR-ROBOT", capabilities={"ROBOTIC_READY"}),
        OperatingRoom("OR-HYB", room_type="HYBRID"),
    ]
    staff = crew("A") + crew("B")
    cases = [
        case("R", "SUR-A", 60, required_capabilities={"ROBOTIC_READY"}),
        case("H", "SUR-B", 60, required_capabilities={"HYBRID"}),
    ]
    _, result = solve(rooms, [], staff, cases)
    assert result.assignments["R"].or_id == "OR-ROBOT"
    assert result.assignments["H"].or_id == "OR-HYB"


def test_impossible_case_is_reported_with_reasons_not_solved():
    rooms = [OperatingRoom("OR-1")]
    staff = crew("A")
    cases = [case("H", "SUR-A", 60, required_capabilities={"HYBRID"}, required_equipment_types=["ROBOT"])]
    _, result = solve(rooms, [], staff, cases)
    reasons = " | ".join(result.unscheduled["H"])
    assert "Facility" in reasons and "Equipment" in reasons


def test_one_person_is_never_in_two_rooms_at_once():
    rooms = [OperatingRoom("OR-1"), OperatingRoom("OR-2")]
    staff = crew("A") + [StaffMember("SUR-B", "Surgeon B", StaffRole.SURGEON, [(hm("08:00"), hm("18:00"))])]
    # Only one anaesthetist / scrub / circulating nurse for two surgeons.
    cases = [
        case("A1", "SUR-A", 120, preferred_start=hm("08:00")),
        case("B1", "SUR-B", 120, preferred_start=hm("08:00")),
    ]
    _, result = solve(rooms, [], staff, cases)
    a, b = result.assignments["A1"], result.assignments["B1"]
    assert a.end_min <= b.start_min or b.end_min <= a.start_min


def test_case_must_fit_inside_a_shift_window():
    rooms = [OperatingRoom("OR-1")]
    split = [(hm("08:00"), hm("10:00")), (hm("14:00"), hm("18:00"))]
    staff = [
        StaffMember("SUR-A", "Surgeon A", StaffRole.SURGEON, split),
        *crew("A")[1:],
    ]
    cases = [case("LONG", "SUR-A", 180, preferred_start=hm("08:00"))]
    _, result = solve(rooms, [], staff, cases)
    assert result.assignments["LONG"].start_min >= hm("14:00")


def test_weekly_hour_cap_prevents_fatigue():
    rooms = [OperatingRoom("OR-1")]
    shift = [(hm("08:00"), hm("18:00"))]
    tired = StaffMember("ANE-TIRED", "Tired", StaffRole.ANESTHESIOLOGIST, shift,
                        max_weekly_hours=48, current_weekly_hours=47)  # 60 min left
    fresh = StaffMember("ANE-FRESH", "Fresh", StaffRole.ANESTHESIOLOGIST, shift)
    staff = [crew("A")[0], tired, fresh, *crew("A")[2:]]
    cases = [case("X", "SUR-A", 90)]
    _, result = solve(rooms, [], staff, cases)
    assert "ANE-FRESH" in result.assignments["X"].staff_ids
    assert "ANE-TIRED" not in result.assignments["X"].staff_ids

    _, result = solve(rooms, [], [s for s in staff if s.staff_id != "ANE-FRESH"], cases)
    assert "ANESTHESIOLOGIST" in result.unscheduled["X"][0]


def test_required_skill_is_enforced():
    rooms = [OperatingRoom("OR-1", capabilities={"ROBOTIC_READY"})]
    shift = [(hm("08:00"), hm("18:00"))]
    staff = [
        *crew("A")[:2], crew("A")[3],
        StaffMember("SCR-PLAIN", "Plain", StaffRole.SCRUB_NURSE, shift),
        StaffMember("SCR-ROBOT", "Robot", StaffRole.SCRUB_NURSE, shift, skills={"ROBOTIC_CERTIFIED"}),
    ]
    cases = [case("R", "SUR-A", 60, required_staff_skills={StaffRole.SCRUB_NURSE: {"ROBOTIC_CERTIFIED"}})]
    _, result = solve(rooms, [], staff, cases)
    assert "SCR-ROBOT" in result.assignments["R"].staff_ids


def test_recovery_beds_are_not_double_booked():
    rooms = [OperatingRoom("OR-1"), OperatingRoom("OR-2")]
    staff = crew("A") + crew("B")
    beds = [RecoveryBed("PACU-1")]
    cases = [
        case("A1", "SUR-A", 60, preferred_start=hm("08:00"), recovery_mins=60),
        case("B1", "SUR-B", 60, preferred_start=hm("08:00"), recovery_mins=60),
    ]
    _, result = solve(rooms, [], staff, cases, beds=beds)
    a, b = sorted(result.assignments.values(), key=lambda x: x.recovery_start_min)
    assert b.recovery_start_min >= a.recovery_end_min


def test_urgent_deadline_is_hard():
    rooms = [OperatingRoom("OR-1")]
    staff = crew("A", "08:00", "18:00")
    ok = case("U", "SUR-A", 60, urgency=Urgency.URGENT, release_min=hm("09:00"), deadline_min=hm("10:00"))
    _, result = solve(rooms, [], staff, [ok])
    assert hm("09:00") <= result.assignments["U"].start_min <= hm("10:00")

    late = case("L", "SUR-A", 60, urgency=Urgency.URGENT, release_min=hm("11:00"), deadline_min=hm("10:00"))
    _, result = solve(rooms, [], staff, [late])
    assert "L" in result.unscheduled


def _one_room_day():
    rooms = [OperatingRoom("OR-1", base_turnover_mins=30)]
    staff = crew("A") + crew("ER", "00:00", "23:59")
    cases = [
        case("E1", "SUR-A", 120, preferred_start=hm("08:00")),
        case("E2", "SUR-A", 120, preferred_start=hm("10:30")),
    ]
    return rooms, staff, cases


def test_emergency_goes_first_and_started_cases_stay_locked():
    rooms, staff, cases = _one_room_day()
    opt, day = solve(rooms, [], staff, cases)
    assert day.assignments["E1"].start_min == hm("08:00")
    assert day.assignments["E2"].start_min == hm("10:30")

    now = hm("09:00")
    er = case("ER", "SUR-ER", 60, urgency=Urgency.EMERGENCY, release_min=now)
    after = opt.insert_emergency(day, cases, [er], now_min=now, verbose=False)
    assert validate_schedule(after, [*cases, er], rooms, [], staff) == []
    locked, before = after.assignments["E1"], day.assignments["E1"]
    assert locked.frozen  # in progress: room, time, team and equipment untouched
    assert (locked.or_id, locked.start_min, locked.end_min, locked.staff_ids, locked.equipment_unit_ids) == (
        before.or_id, before.start_min, before.end_min, before.staff_ids, before.equipment_unit_ids
    )
    assert after.assignments["ER"].start_min == hm("10:30")  # first moment OR-1 is clean
    assert after.assignments["E2"].start_min >= after.assignments["ER"].cleanup_end_min
    changed = {c.case_id for c in after.changes}
    assert changed == {"ER", "E2"}
    assert {n.staff_id for n in after.notifications} >= {"SUR-A", "SUR-ER"}


def test_overrun_pushes_following_case():
    rooms, staff, cases = _one_room_day()
    opt, day = solve(rooms, [], staff, cases)
    after = opt.handle_overrun(day, cases, "E1", new_duration_mins=180, now_min=hm("09:30"), verbose=False)
    e1, e2 = after.assignments["E1"], after.assignments["E2"]
    assert e1.end_min == hm("11:00") and e1.frozen
    assert e2.start_min == hm("11:30")
    assert validate_schedule(after, [*cases[1:], after.cases["E1"]], rooms, [], staff) == []


def test_validator_detects_double_booking():
    rooms = [OperatingRoom("OR-1")]
    staff = crew("A") + crew("B")
    cases = [case("A1", "SUR-A", 60), case("B1", "SUR-B", 60)]
    clash = {
        "A1": Assignment("A1", "OR-1", hm("08:00"), hm("09:00"), hm("09:30"),
                         ("SUR-A", "ANE-A", "SCR-A", "CIR-A")),
        "B1": Assignment("B1", "OR-1", hm("09:00"), hm("10:00"), hm("10:30"),
                         ("SUR-B", "ANE-B", "SCR-B", "CIR-B")),
    }
    errors = validate_schedule(clash, cases, rooms, [], staff)
    assert any("room OR-1 double-booked" in e for e in errors)


def test_duplicate_ids_are_rejected():
    with pytest.raises(ValueError):
        ORchestraOptimizer([OperatingRoom("OR-1"), OperatingRoom("OR-1")], [], [])
