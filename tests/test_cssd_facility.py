"""Equipment & CSSD Agent (work queue, planned maintenance) and OR Facility
Agent (elective hours, recovery beds, recovery time per case)."""
from __future__ import annotations

from orchestra_engine import (
    EquipmentUnit,
    ORchestraOptimizer,
    OperatingRoom,
    RecoveryBed,
    StaffMember,
    StaffRole,
    SurgeryCase,
    Urgency,
    cssd_queue,
    hm,
    validate_schedule,
)

SHIFT = [(hm("06:00"), hm("23:00"))]


def team(tag: str) -> list[StaffMember]:
    return [StaffMember(f"{role[:3]}-{tag}", f"{role} {tag}", role, SHIFT) for role in (
        StaffRole.SURGEON, StaffRole.ANESTHESIOLOGIST, StaffRole.SCRUB_NURSE, StaffRole.CIRCULATING_NURSE)]


def case(cid, surgeon="SUR-A", minutes=60, **kw) -> SurgeryCase:
    return SurgeryCase(cid, f"HN-{cid}", f"Procedure {cid}", surgeon, minutes, **kw)


STAFF = team("A") + team("B")


def solve(rooms, cases, units=(), beds=()):
    opt = ORchestraOptimizer(rooms, units, STAFF, beds, time_limit_s=10, num_workers=4)
    result = opt.solve_schedule(cases, "facility", verbose=False)
    assert validate_schedule(result, cases, rooms, units, STAFF, beds) == []
    return opt, result


# ------------------------------------------------------------- OR Facility

def test_elective_hours_bind_elective_cases_only():
    rooms = [OperatingRoom("OR-1", elective_start=hm("08:00"), elective_end=hm("16:00"))]
    _, result = solve(rooms, [case("EL", minutes=120, preferred_start=hm("15:00"))])
    assert result.assignments["EL"].end_min <= hm("16:00")
    _, result = solve(rooms, [case("LATE", minutes=120, release_min=hm("15:00"))])
    assert "เวลาผ่าตัดนัดของห้อง" in result.unscheduled["LATE"][0] and "OR-1 08:00-16:00" in result.unscheduled["LATE"][0]
    _, result = solve(rooms, [case("UR", minutes=120, urgency=Urgency.URGENT, release_min=hm("17:00"))])
    assert result.assignments["UR"].start_min == hm("17:00")  # urgent cases may use the evening


def test_slot_scan_explains_elective_hours():
    rooms = [OperatingRoom("OR-1", elective_start=hm("08:00"), elective_end=hm("16:00"))]
    opt, day = solve(rooms, [])
    check = opt.check_slot(day, case("X"), hm("07:00"), verbose=False)
    facility = next(v for v in check.verdicts if v.agent == "OR Facility Agent")
    assert "เวลาผ่าตัดนัดของห้องคือ 08:00-16:00" in facility.items[0].note
    assert check.assignment.start_min == hm("08:00")


def test_closed_or_occupied_beds_are_not_used():
    rooms = [OperatingRoom("OR-1")]
    beds = [RecoveryBed("ICU-1", "ICU_SURGICAL", status="CLOSED"),
            RecoveryBed("ICU-2", "ICU_SURGICAL", available_from=hm("14:00"))]  # a ward patient until 14:00
    _, result = solve(rooms, [case("BIG", minutes=120, requires_icu_post_op=True)], beds=beds)
    a = result.assignments["BIG"]
    assert a.bed_id == "ICU-2" and a.end_min >= hm("14:00")
    _, result = solve(rooms, [case("BIG", minutes=120, requires_icu_post_op=True)], beds=beds[:1])
    assert "ปิด: ICU-1" in result.unscheduled["BIG"][0]


def test_recovery_time_per_case_holds_the_bed():
    rooms = [OperatingRoom("OR-1"), OperatingRoom("OR-2")]
    beds = [RecoveryBed("PACU-1")]
    cases = [case("A", recovery_mins=180, preferred_start=hm("08:00")),
             case("B", "SUR-B", recovery_mins=30, preferred_start=hm("08:00"))]
    _, result = solve(rooms, cases, beds=beds)
    a, b = result.assignments["A"], result.assignments["B"]
    assert a.recovery_end_min - a.recovery_start_min == 180
    first, second = sorted([a, b], key=lambda x: x.end_min)
    assert second.recovery_start_min >= first.recovery_end_min  # one PACU bed, one patient at a time


# ------------------------------------------------------------ Equipment & CSSD

def test_planned_maintenance_keeps_the_machine_free():
    rooms = [OperatingRoom("OR-1")]
    robot = [EquipmentUnit("DV-01", "ROBOT", 0, False, closures=[(hm("08:00"), hm("12:00"), "service visit")])]
    _, result = solve(rooms, [case("R", minutes=120, required_equipment_types=["ROBOT"], preferred_start=hm("08:00"))],
                      units=robot)
    a = result.assignments["R"]
    assert a.end_min <= hm("08:00") or a.start_min >= hm("12:00")  # never during the service visit
    whole_day = [EquipmentUnit("DV-01", "ROBOT", 0, False, closures=[(0, 24 * 60, "sent for repair")])]
    _, result = solve(rooms, [case("R", required_equipment_types=["ROBOT"])], units=whole_day)
    assert "นัดซ่อมบำรุง: DV-01 00:00" in result.unscheduled["R"][0] and "sent for repair" in result.unscheduled["R"][0]
    opt, day = solve(rooms, [], units=robot)
    check = opt.check_slot(day, case("R2", required_equipment_types=["ROBOT"]), hm("09:00"), verbose=False)
    equipment = next(v for v in check.verdicts if v.agent == "Equipment & CSSD Agent")
    assert "นัดซ่อมบำรุง 08:00-12:00 (service visit)" in equipment.items[0].note


def test_cssd_queue_orders_trays_by_slack():
    rooms = [OperatingRoom("OR-1"), OperatingRoom("OR-2")]
    trays = [EquipmentUnit("TRAY-1", "TRAY", 90)]
    cases = [case("A", required_equipment_types=["TRAY"], preferred_start=hm("08:00")),
             case("B", "SUR-B", required_equipment_types=["TRAY"], preferred_start=hm("10:30"))]
    _, result = solve(rooms, cases, units=trays)
    [job, last] = cssd_queue(result.assignments, trays, now_min=hm("09:05"))
    assert (job["from_case"], job["ready"], job["next_case"], job["slack_mins"]) == ("A", "10:30", "B", 0)
    assert job["level"] == "tight" and job["state"] == "processing"
    assert last["from_case"] == "B" and last["level"] == "free"

    # CSSD reports the tray will only be ready at 11:00: the next case is now late.
    trays[0].status, trays[0].available_at = "STERILIZING", hm("11:00")
    [job, _] = cssd_queue(result.assignments, trays, now_min=hm("09:05"))
    assert job["reported"] and job["ready"] == "11:00" and job["slack_mins"] == -30 and job["level"] == "late"
    errors = validate_schedule(result, cases, rooms, trays, STAFF)
    assert any("TRAY-1 ยังไม่กลับจาก CSSD จนถึง 11:00" in e for e in errors)
