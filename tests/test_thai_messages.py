"""What the agents tell people is Thai.

Codes (rooms, units, beds, supplies, equipment types), names of people and
procedures, and the agents' own names stay as they are; every other word in
a verdict, reason, log line, warning or change is Thai.
"""
from __future__ import annotations

import re

from app.store import ScheduleStore
from orchestra_engine import (EquipmentUnit, OperatingRoom, ORchestraOptimizer, RecoveryBed, StaffMember, StaffRole,
                              SupplyItem, SurgeryCase, Urgency, hm, validate_schedule)
from orchestra_engine.timeutil import fmt

ENGLISH = re.compile(r"\b[a-z]{3,}\b")  # lower-case English words (codes are upper-case)
AGENT_NAMES = ("Staffing Agent", "Equipment & CSSD Agent", "OR Facility Agent", "Master Orchestrator",
               "Intake -> Master", "Intake & Triage Agent", "Solver", "CP-SAT")
NURSE = {"name": "Nurse", "username": "n", "role": "admin", "staff_id": None, "user_id": "u"}
# Names and codes, not messages: procedure and people's names, activity kind, who did it.
CODE_FIELDS = {"procedure", "name", "patient_name", "scenario", "title", "kind", "actor", "status", "urgency",
               "infection_risk", "staff_id", "case_id"}


def _english(texts, names=()) -> set[str]:
    found = set()
    for text in texts:
        for name in sorted({*AGENT_NAMES, *names}, key=len, reverse=True):
            text = text.replace(name, "")
        found |= set(ENGLISH.findall(text))
    return found


def _texts(out) -> list[str]:
    """Every string inside a nested result."""
    if isinstance(out, str):
        return [out]
    if isinstance(out, dict):
        return [t for k, v in out.items() if k not in CODE_FIELDS for t in _texts(v)]
    if isinstance(out, (list, tuple)):
        return [t for v in out for t in _texts(v)]
    return []


def test_the_check_finds_english():
    assert _english(["[Staffing Agent] OR-1: busy with C01 until 10:30"]) == {"busy", "with", "until"}
    assert _english(["[Staffing Agent] OR-1: ติดเคส C01 ถึง 10:30 (Dr.A)"], ["Dr.A"]) == set()


def test_next_day_times_are_thai():
    assert [fmt(m) for m in (600, 24 * 60 + 480, 2 * 24 * 60 + 60, -30)] == [
        "10:00", "08:00 (พรุ่งนี้)", "01:00 (+2 วัน)", "23:30 (เมื่อวาน)"]


def test_demo_day_messages_are_thai():
    s = ScheduleStore(workers=2, time_limit_s=10)
    state = s.state()
    names = [p.name for p in s.hospital.staff] + [x.name for x in s.hospital.supplies]
    names += [b["procedure"] for b in state["schedule"]["bookings"]] + [p["name"] for p in state["options"]["procedures"]]
    first = state["schedule"]["bookings"][0]
    case = {"procedure": first["procedure"], "patient_hn": "HN-T1", "surgeon_id": first["team"][0]["staff_id"],
            "duration_mins": 90, "required_equipment_types": ["LAPAROSCOPY_TRAY"], "required_supplies": {"ROBO-DRAPE": 5}}
    texts = _texts(s.slot_check(case, first["start"], commit=False, actor=NURSE))  # busy surgeon, no drapes
    texts += _texts(s.slot_check({**case, "required_supplies": {}}, first["start"], commit=True, actor=NURSE))
    texts += _texts(s.emergency({**case, "patient_hn": "HN-T2", "required_supplies": {}}, "09:00", actor=NURSE))
    texts += _texts(s.overrun(first["case_id"], first["end_min"] - first["start_min"] + 120, "09:30", actor=NURSE))
    texts += _texts(s.replan(actor=NURSE))
    texts += _texts(s.state()["activity"]) + _texts(s.state()["warnings"]) + _texts(s.state()["fatigue"])
    assert len(texts) > 50
    assert _english(texts, names) == set()


def test_engine_reasons_and_warnings_are_thai():
    """Situations the demo day does not reach: closures, maintenance, rest, stock, beds, skills."""
    hours = [(hm("07:00"), hm("19:00"))]
    staff = [StaffMember("SUR-A", "Surgeon A", StaffRole.SURGEON, shifts=hours, rest_until=hm("09:00")),
             StaffMember("ANE-A", "Anes A", StaffRole.ANESTHESIOLOGIST, shifts=hours, max_daily_hours=1),
             StaffMember("SCR-A", "Scrub A", StaffRole.SCRUB_NURSE, shifts=hours),
             StaffMember("CIR-A", "Circ A", StaffRole.CIRCULATING_NURSE, shifts=hours)]
    rooms = [OperatingRoom("OR-1", elective_start=hm("08:00"), elective_end=hm("12:00"),
                           closures=[(hm("12:00"), hm("13:00"), "x")]),
             OperatingRoom("OR-2", status="MAINTENANCE")]
    units = [EquipmentUnit("T-1", "TRAY", 60, False, closures=[(hm("07:00"), hm("10:00"), "x")]),
             EquipmentUnit("T-2", "TRAY", 60, False, status="STERILIZING", available_at=hm("11:00"))]
    beds = [RecoveryBed("P-1", "PACU", available_from=hm("13:00")), RecoveryBed("I-1", "ICU_SURGICAL", status="CLOSED")]
    stock = [SupplyItem("MESH", "Mesh", 0, note="x")]
    opt = ORchestraOptimizer(rooms, units, staff, beds, supplies=stock, time_limit_s=10, num_workers=2)
    cases = [
        SurgeryCase("A", "HN", "x", "SUR-A", 120, required_equipment_types=["TRAY"]),
        SurgeryCase("B", "HN", "x", "SUR-A", 300),
        SurgeryCase("C", "HN", "x", "SUR-A", 60, required_supplies={"MESH": 1}, requires_icu_post_op=True),
        SurgeryCase("D", "HN", "x", "SUR-A", 60, required_capabilities={"HYBRID"},
                    required_staff_skills={"SCRUB_NURSE": frozenset({"CARDIAC"})}),
        SurgeryCase("E", "HN", "x", "SUR-A", 60, urgency=Urgency.EMERGENCY, required_equipment_types=["ROBOT"]),
    ]
    day = opt.solve_schedule(cases, "t", verbose=False)
    texts = [r for reasons in day.unscheduled.values() for r in reasons]
    texts += [w for a in day.assignments.values() for w in a.reasoning]
    for start in ("07:00", "08:30", "11:30"):
        check = opt.check_slot(day, SurgeryCase("N", "HN", "x", "SUR-A", 90, required_equipment_types=["TRAY"]),
                               hm(start), verbose=False)
        texts += check.log + [i.note for v in check.verdicts for i in v.items]
    late = {"A": day.assignments.get("A")} if "A" in day.assignments else {}
    texts += validate_schedule(late, cases, [OperatingRoom("OR-1", status="CLOSED")], units, staff, beds, supplies=stock)
    assert len(texts) > 10
    assert _english(texts, ["Surgeon A", "Anes A", "Scrub A", "Circ A"]) == set()
