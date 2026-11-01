"""Demo hospital and scenarios (synthetic data only).

Run:  python -m orchestra_engine.demo
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .models import (
    BedType,
    EquipmentUnit,
    InfectionRisk,
    OperatingRoom,
    RecoveryBed,
    StaffMember,
    StaffRole,
    SurgeryCase,
    Urgency,
)
from .optimizer import ORchestraOptimizer
from .results import ScheduleResult
from .timeutil import hm
from .validation import validate_schedule

ROBOTIC = {StaffRole.SCRUB_NURSE: {"ROBOTIC_CERTIFIED"}}


@dataclass
class Hospital:
    rooms: list[OperatingRoom]
    equipments: list[EquipmentUnit]
    staff: list[StaffMember]
    beds: list[RecoveryBed]
    team_synergy: dict[tuple[str, str], float]

    def optimizer(self, **kwargs) -> ORchestraOptimizer:
        kwargs.setdefault("time_limit_s", 10.0)
        return ORchestraOptimizer(
            self.rooms, self.equipments, self.staff, self.beds,
            team_synergy=self.team_synergy, **kwargs,
        )

    def validate(self, result: ScheduleResult) -> list[str]:
        return validate_schedule(
            result, result.cases.values(), self.rooms, self.equipments, self.staff, self.beds
        )


def _person(staff_id, name, role, start, end, skills=(), used=24.0, more_shifts=()):
    shifts = [(hm(start), hm(end)), *more_shifts]
    return StaffMember(staff_id, name, role, shifts, skills=skills, current_weekly_hours=used)


def build_hospital() -> Hospital:
    rooms = [
        OperatingRoom("OR-1", "STANDARD", {"LAMINAR_FLOW"}),
        OperatingRoom("OR-2", "STANDARD"),
        OperatingRoom("OR-3", "STANDARD", {"ROBOTIC_READY"}),
        OperatingRoom("OR-4", "HYBRID", {"LEAD_LINED_XRAY"}),
    ]

    def units(prefix, eq_type, n, sterilization=120, needs_cssd=True):
        return [
            EquipmentUnit(f"{prefix}-{i:02d}", eq_type, sterilization, needs_cssd)
            for i in range(1, n + 1)
        ]

    equipments = [
        *units("DV", "ROBOT_DA_VINCI", 1, sterilization=60),
        *units("RIS", "ROBOTIC_INSTRUMENT_SET", 2),
        *units("CARM", "C_ARM", 2, needs_cssd=False),
        *units("KNEE", "ORTHO_KNEE_SET", 2),
        *units("ARTH", "ARTHROSCOPY_SET", 1, sterilization=90),
        *units("LAP", "LAPAROSCOPY_TRAY", 3, sterilization=90),
        *units("MAJ", "MAJOR_GENERAL_TRAY", 4),
        *units("VASC", "VASCULAR_TRAY", 1),
    ]

    S, A = StaffRole.SURGEON, StaffRole.ANESTHESIOLOGIST
    SN, CN = StaffRole.SCRUB_NURSE, StaffRole.CIRCULATING_NURSE
    staff = [
        _person("SUR-ORTHO", "Dr.Somchai (Ortho)", S, "08:00", "17:00"),
        _person("SUR-GEN", "Dr.Napat (General)", S, "08:00", "16:00"),
        _person("SUR-URO", "Dr.Kittipong (Uro)", S, "08:00", "17:00", {"ROBOTIC_CERTIFIED"}),
        _person("SUR-VASC", "Dr.Pimchanok (Vascular)", S, "08:00", "17:00"),
        _person("SUR-ONCALL", "Dr.Anan (General, on-call)", S, "08:00", "23:59"),
        _person("ANE-1", "Dr.Wanida", A, "08:00", "16:00"),
        _person("ANE-2", "Dr.Chaiwat", A, "08:00", "16:00", used=45),  # only 3 h left
        _person("ANE-3", "Dr.Ratana", A, "10:00", "18:00", {"PEDIATRIC_ANESTHESIA"}),
        _person("ANE-4", "Dr.Suda", A, "08:00", "16:00"),
        _person("ANE-ONCALL", "Dr.Prasert (on-call)", A, "08:00", "23:59"),
        _person("SCR-1", "Malee", SN, "08:00", "16:00", {"ROBOTIC_CERTIFIED"}),
        _person("SCR-2", "Kanya", SN, "08:00", "16:00"),
        _person("SCR-3", "Orathai", SN, "08:00", "16:00"),
        _person("SCR-4", "Jintana", SN, "10:00", "18:00", {"ROBOTIC_CERTIFIED"}),
        _person("SCR-5", "Sirilak", SN, "08:00", "16:00"),
        _person("SCR-ONCALL", "Duangjai (on-call)", SN, "08:00", "23:59"),
        _person("CIR-1", "Nok", CN, "08:00", "16:00"),
        _person("CIR-2", "Ploy", CN, "08:00", "16:00"),
        _person("CIR-3", "Fon", CN, "08:00", "16:00"),
        _person("CIR-4", "Dao", CN, "10:00", "18:00", {"PEDIATRIC_CARE"}),
        _person("CIR-ONCALL", "Fah (on-call)", CN, "08:00", "23:59"),
    ]
    beds = [RecoveryBed(f"PACU-{i}", BedType.PACU) for i in range(1, 5)] + [
        RecoveryBed(f"ICU-{i}", BedType.ICU_SURGICAL) for i in range(1, 3)
    ]
    synergy = {
        ("SUR-ORTHO", "SCR-2"): 4.5,
        ("SUR-URO", "SCR-1"): 4.8,
        ("SUR-URO", "ANE-1"): 4.0,
        ("SUR-GEN", "SCR-3"): 3.5,
        ("SUR-VASC", "ANE-4"): 4.2,
    }
    return Hospital(rooms, equipments, staff, beds, synergy)


def elective_day() -> list[SurgeryCase]:
    return [
        SurgeryCase("C01", "HN-0001", "Total knee arthroplasty (L)", "SUR-ORTHO", 150,
                    required_capabilities={"LAMINAR_FLOW"},
                    required_equipment_types=["ORTHO_KNEE_SET", "C_ARM"], preferred_start=hm("08:00")),
        SurgeryCase("C02", "HN-0002", "Total knee arthroplasty (R)", "SUR-ORTHO", 150,
                    required_capabilities={"LAMINAR_FLOW"},
                    required_equipment_types=["ORTHO_KNEE_SET", "C_ARM"], preferred_start=hm("11:00")),
        SurgeryCase("C03", "HN-0003", "Arthroscopic ACL reconstruction", "SUR-ORTHO", 90,
                    required_capabilities={"LAMINAR_FLOW"}, required_equipment_types=["ARTHROSCOPY_SET"]),
        SurgeryCase("C04", "HN-0004", "Robotic radical prostatectomy", "SUR-URO", 210,
                    required_capabilities={"ROBOTIC_READY"}, required_staff_skills=ROBOTIC,
                    required_equipment_types=["ROBOT_DA_VINCI", "ROBOTIC_INSTRUMENT_SET"],
                    preferred_start=hm("08:00")),
        SurgeryCase("C05", "HN-0005", "Robotic partial nephrectomy", "SUR-URO", 180,
                    required_capabilities={"ROBOTIC_READY"}, required_staff_skills=ROBOTIC,
                    required_equipment_types=["ROBOT_DA_VINCI", "ROBOTIC_INSTRUMENT_SET"]),
        SurgeryCase("C06", "HN-0006", "Laparoscopic cholecystectomy", "SUR-GEN", 75,
                    required_equipment_types=["LAPAROSCOPY_TRAY"], preferred_start=hm("08:00")),
        SurgeryCase("C07", "HN-0007", "Laparoscopic appendectomy", "SUR-GEN", 60,
                    urgency=Urgency.URGENT, required_equipment_types=["LAPAROSCOPY_TRAY"],
                    release_min=hm("08:00"), deadline_min=hm("12:00")),
        SurgeryCase("C08", "HN-0008", "Open inguinal hernia repair", "SUR-GEN", 90,
                    required_equipment_types=["MAJOR_GENERAL_TRAY"]),
        SurgeryCase("C09", "HN-0009", "Incision and drainage of abscess", "SUR-GEN", 45,
                    required_equipment_types=["MAJOR_GENERAL_TRAY"], infection_risk=InfectionRisk.DIRTY),
        SurgeryCase("C10", "HN-0010", "Femoral-popliteal bypass", "SUR-VASC", 240,
                    required_capabilities={"LEAD_LINED_XRAY"},
                    required_equipment_types=["VASCULAR_TRAY", "C_ARM"], requires_icu_post_op=True,
                    preferred_start=hm("08:00")),
    ]


def emergency_laparotomy(now: int) -> SurgeryCase:
    return SurgeryCase(
        "E01", "HN-9001", "Exploratory laparotomy (perforated viscus)", "SUR-ONCALL", 120,
        urgency=Urgency.EMERGENCY, required_equipment_types=["MAJOR_GENERAL_TRAY"],
        infection_risk=InfectionRisk.DIRTY, requires_icu_post_op=True, release_min=now,
    )


def run_all(verbose: bool = True) -> dict[str, ScheduleResult]:
    hospital = build_hospital()
    opt = hospital.optimizer()
    cases = elective_day()
    results: dict[str, ScheduleResult] = {}

    # 1) Normal elective day.
    day = opt.solve_schedule(cases, "1) Normal elective day", verbose=verbose)
    results["day"] = day

    # 2) CSSD conflict: one knee set, still in CSSD until 09:30, two TKAs wanted at 08:00.
    cssd = build_hospital()
    cssd.equipments = [e for e in cssd.equipments if e.unit_id != "KNEE-02"]
    for unit in cssd.equipments:
        if unit.unit_id == "KNEE-01":
            unit.status, unit.available_at = "STERILIZING", hm("09:30")
    tkas = [replace(c, preferred_start=hm("08:00")) for c in cases if c.case_id in ("C01", "C02")]
    results["cssd"] = cssd.optimizer().solve_schedule(
        tkas, "2) CSSD conflict: one knee set still sterilizing", verbose=verbose
    )

    # 3) Emergency arrives at 10:30 while the day is running.
    now = hm("10:30")
    results["emergency"] = opt.insert_emergency(
        day, cases, [emergency_laparotomy(now)], now_min=now,
        scenario_name="3) EMERGENCY laparotomy at 10:30 (ripple effect)", verbose=verbose,
    )

    # 4) At 11:00 the robotic prostatectomy (C04) will run 60 min over.
    after = results["emergency"]
    overrun_at = hm("11:00")
    c04 = after.assignments["C04"]
    results["overrun"] = opt.handle_overrun(
        after, [*cases, emergency_laparotomy(now)], "C04",
        new_duration_mins=(c04.end_min - c04.start_min) + 60, now_min=overrun_at,
        scenario_name="4) C04 overruns by 60 min (noticed at 11:00)", verbose=verbose,
    )

    if verbose:
        checks = {
            "day": hospital, "cssd": cssd, "emergency": hospital, "overrun": hospital,
        }
        print("=== Independent hard-constraint check ===")
        for key, result in results.items():
            errors = checks[key].validate(result)
            print(f"{result.scenario}: {'OK, no conflicts' if not errors else errors}")
    return results


if __name__ == "__main__":
    run_all()
