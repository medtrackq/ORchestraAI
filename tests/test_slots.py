from __future__ import annotations

import json
import re
import shutil

from orchestra_engine import (
    EquipmentUnit,
    ORchestraOptimizer,
    OperatingRoom,
    RecoveryBed,
    StaffMember,
    StaffRole,
    SurgeryCase,
    hm,
    validate_schedule,
)
from orchestra_engine.agents import EQUIPMENT, FACILITY, STAFFING
from orchestra_engine.flow import PAGE, build_flow, write_page


def _world():
    shift = [(hm("08:00"), hm("18:00"))]
    staff = [
        StaffMember("SUR-A", "Surgeon A", StaffRole.SURGEON, shift),
        StaffMember("SUR-B", "Surgeon B", StaffRole.SURGEON, shift),
        StaffMember("ANE-1", "Anesth 1", StaffRole.ANESTHESIOLOGIST, shift),
        StaffMember("ANE-2", "Anesth 2", StaffRole.ANESTHESIOLOGIST, shift),
        StaffMember("SCR-1", "Scrub 1", StaffRole.SCRUB_NURSE, shift),
        StaffMember("SCR-2", "Scrub 2", StaffRole.SCRUB_NURSE, shift),
        StaffMember("CIR-1", "Circ 1", StaffRole.CIRCULATING_NURSE, shift),
        StaffMember("CIR-2", "Circ 2", StaffRole.CIRCULATING_NURSE, shift),
    ]
    rooms = [OperatingRoom("OR-1"), OperatingRoom("OR-2", capabilities={"ROBOTIC_READY"})]
    units = [
        EquipmentUnit("SET-1", "KNEE_SET", sterilization_mins=120, status="STERILIZING", available_at=hm("12:00")),
        EquipmentUnit("TRAY-1", "BASIC_TRAY", sterilization_mins=60),
    ]
    beds = [RecoveryBed("PACU-1"), RecoveryBed("PACU-2")]
    opt = ORchestraOptimizer(rooms, units, staff, beds, time_limit_s=10)
    booked_case = SurgeryCase("B1", "HN-B1", "Booked", "SUR-B", 120,
                              required_equipment_types=["BASIC_TRAY"], preferred_start=hm("08:00"))
    booked = opt.solve_schedule([booked_case], "booked", verbose=False)
    return opt, rooms, units, staff, beds, booked


def _valid(result, rooms, units, staff, beds):
    assert validate_schedule(result, result.cases.values(), rooms, units, staff, beds) == []


def test_slot_is_locked_when_every_agent_agrees():
    opt, rooms, units, staff, beds, booked = _world()
    case = SurgeryCase("N1", "HN-N1", "New", "SUR-A", 60)
    check = opt.check_slot(booked, case, hm("09:00"), verbose=False)
    assert check.ok and all(v.ok for v in check.verdicts)
    assert check.assignment.start_min == hm("09:00")
    assert check.result.assignments["B1"] == booked.assignments["B1"]  # nothing moved
    assert [c.case_id for c in check.result.changes] == ["N1"]
    _valid(check.result, rooms, units, staff, beds)


def test_equipment_conflict_is_negotiated_to_next_ready_slot():
    opt, rooms, units, staff, beds, booked = _world()
    case = SurgeryCase("N1", "HN-N1", "Knee", "SUR-A", 90, required_equipment_types=["KNEE_SET"])
    check = opt.check_slot(booked, case, hm("09:00"), verbose=False)
    verdicts = {v.agent: v for v in check.verdicts}
    assert verdicts[STAFFING].ok and verdicts[FACILITY].ok
    assert not verdicts[EQUIPMENT].ok
    assert verdicts[EQUIPMENT].ready_at == hm("12:00")
    assert "sterilizing" in verdicts[EQUIPMENT].summary()
    assert check.assignment.start_min == hm("12:00")
    assert check.result.assignments["B1"] == booked.assignments["B1"]
    assert any("rejected by Equipment" in line for line in check.log)
    _valid(check.result, rooms, units, staff, beds)


def test_busy_room_and_surgeon_are_reported_with_the_blocking_case():
    opt, rooms, units, staff, beds, booked = _world()
    case = SurgeryCase("N1", "HN-N1", "Same surgeon", "SUR-B", 60, required_capabilities={"ROBOTIC_READY"})
    b1 = booked.assignments["B1"]
    check = opt.check_slot(booked, case, b1.start_min, verbose=False)
    verdicts = {v.agent: v for v in check.verdicts}
    assert not verdicts[STAFFING].ok and "busy with B1" in verdicts[STAFFING].summary()
    assert check.assignment.start_min >= b1.end_min
    _valid(check.result, rooms, units, staff, beds)


def test_impossible_request_returns_reasons():
    opt, _, _, _, _, booked = _world()
    case = SurgeryCase("N1", "HN-N1", "Hybrid only", "SUR-A", 60, required_capabilities={"HYBRID"})
    check = opt.check_slot(booked, case, hm("09:00"), verbose=False)
    assert check.assignment is None
    assert "N1" in check.result.unscheduled
    assert any("no conflict-free slot" in line for line in check.log)


def test_results_export_to_json():
    opt, _, _, _, _, booked = _world()
    check = opt.check_slot(booked, SurgeryCase("N1", "HN-N1", "New", "SUR-A", 60), hm("09:00"), verbose=False)
    text = json.dumps(check.to_dict())
    assert '"case_id": "N1"' in text
    assert json.loads(json.dumps(booked.to_dict()))["bookings"][0]["case_id"] == "B1"


def test_flow_page_data_follows_the_engine(tmp_path):
    data = build_flow()  # raises if any schedule fails the independent validator
    steps = data["steps"]
    assert len(steps) == 6
    proposal = next(b for b in steps[3]["blocks"] if b["case_id"] == "N01")
    assert proposal["kind"] == "moving" and proposal["start"] == hm("12:30")
    final = {b["case_id"]: b for b in steps[5]["blocks"]}
    assert final["E01"]["kind"] == "emergency"
    assert final["N01"]["kind"] == "moved" and final["N01"]["start"] > hm("12:30")

    page = tmp_path / "flow.html"
    shutil.copy(PAGE, page)
    write_page(data, page)
    html = page.read_text(encoding="utf-8")
    block = re.search(r'<script id="flow-data" type="application/json">(.*?)</script>', html, re.S).group(1)
    embedded = json.loads(block)
    assert [s["title"] for s in embedded["steps"]] == [s["title"] for s in steps]
