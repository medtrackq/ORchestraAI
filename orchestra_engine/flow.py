"""Real engine data for the interactive flow page ``web/orchestra-flow.html``.

The page follows the five steps of the ORchestraAI workflow (intake, parallel
scan, negotiation, lock, emergency). Every number, state and log line on it
comes from running the engine on the synthetic scenario below.

    python -m orchestra_engine.flow                 # refresh the page's data block
    python -m orchestra_engine.flow --print         # print the agents' log only
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from . import thai
from .agents import EQUIPMENT, FACILITY, STAFFING, AgentVerdict, Busy, SlotCheck, scan_slot
from .demo import Hospital, build_hospital, elective_day
from .models import EquipmentUnit, OperatingRoom, StaffMember, StaffRole, SurgeryCase, Urgency
from .results import ScheduleResult
from .timeutil import fmt, hm

PAGE = Path(__file__).resolve().parent.parent / "web" / "orchestra-flow.html"
_DATA_BLOCK = re.compile(r'(<script id="flow-data" type="application/json">)(.*?)(</script>)', re.S)

REQUEST_AT = hm("09:00")
EMERGENCY_AT = hm("12:15")


def build_flow_hospital() -> Hospital:
    """Three rooms like the original mock-up; OR-3 is the hybrid robot room."""
    h = build_hospital()
    h.rooms = [
        OperatingRoom("OR-1", "STANDARD", {"LAPAROSCOPY_TOWER"}),
        OperatingRoom("OR-2", "STANDARD", {"ROBOTIC_READY", "UROLOGY_TABLE"}),
        OperatingRoom("OR-3", "HYBRID", {"ROBOTIC_READY", "LAMINAR_FLOW", "LEAD_LINED_XRAY"}),
    ]
    for unit in h.equipments:
        if unit.unit_id == "KNEE-01":  # the only knee set left is still in CSSD
            unit.status, unit.available_at = "STERILIZING", hm("12:30")
        elif unit.unit_id == "KNEE-02":
            unit.status = "MAINTENANCE"
    h.equipments.append(EquipmentUnit("CRANI-01", "NEURO_CRANIOTOMY_SET", 120))
    for person in h.staff:
        if person.staff_id == "SUR-ORTHO":
            person.shifts = ((hm("08:00"), hm("18:00")),)
    h.staff.append(
        StaffMember("SUR-NEURO", "Dr.Thanakorn (Neuro, on-call)", StaffRole.SURGEON,
                    [(hm("08:00"), hm("23:59"))], current_weekly_hours=20)
    )
    return h


def booked_cases() -> list[SurgeryCase]:
    """Cases already on the books before the new request arrives."""
    lap = {"LAPAROSCOPY_TOWER"}
    day = {c.case_id: c for c in elective_day()}
    c04 = day["C04"]
    return [
        SurgeryCase("C06", "HN-0006", "Laparoscopic cholecystectomy", "SUR-GEN", 75,
                    required_capabilities=lap, required_equipment_types=["LAPAROSCOPY_TRAY"],
                    preferred_start=hm("08:00")),
        SurgeryCase("C07", "HN-0007", "Laparoscopic appendectomy", "SUR-GEN", 60, urgency=Urgency.URGENT,
                    required_capabilities=lap, required_equipment_types=["LAPAROSCOPY_TRAY"],
                    release_min=hm("08:00"), deadline_min=hm("12:00")),
        SurgeryCase("C08", "HN-0008", "Laparoscopic inguinal hernia repair (TEP)", "SUR-GEN", 90,
                    required_capabilities=lap, required_equipment_types=["LAPAROSCOPY_TRAY"],
                    preferred_start=hm("11:30")),
        SurgeryCase("C11", "HN-0011", "Laparoscopic sleeve gastrectomy", "SUR-GEN", 120,
                    required_capabilities=lap, required_equipment_types=["LAPAROSCOPY_TRAY"],
                    preferred_start=hm("13:30")),
        SurgeryCase(c04.case_id, c04.patient_hn, c04.procedure, c04.surgeon_id, c04.duration_mins,
                    required_capabilities={"ROBOTIC_READY", "UROLOGY_TABLE"},
                    required_staff_skills=c04.required_staff_skills,
                    required_equipment_types=c04.required_equipment_types, preferred_start=hm("08:00")),
        SurgeryCase("U01", "HN-0012", "Transurethral resection of prostate (TURP)", "SUR-URO", 60,
                    required_capabilities={"UROLOGY_TABLE"}, required_equipment_types=["MAJOR_GENERAL_TRAY"],
                    preferred_start=hm("12:00")),
        SurgeryCase("U02", "HN-0013", "Ureteroscopy with laser lithotripsy", "SUR-URO", 60,
                    required_capabilities={"UROLOGY_TABLE"}, required_equipment_types=["MAJOR_GENERAL_TRAY"],
                    preferred_start=hm("13:30")),
    ]


def new_request() -> SurgeryCase:
    return SurgeryCase(
        "N01", "HN-0101", "Robotic-assisted total knee arthroplasty", "SUR-ORTHO", 150,
        required_capabilities={"ROBOTIC_READY", "LAMINAR_FLOW"},
        required_equipment_types=["ROBOT_DA_VINCI", "ORTHO_KNEE_SET"], preferred_start=REQUEST_AT,
    )


def emergency_case() -> SurgeryCase:
    return SurgeryCase(
        "E01", "HN-9002", "Emergency craniotomy (traumatic subdural haematoma)", "SUR-NEURO", 120,
        urgency=Urgency.EMERGENCY, required_equipment_types=["NEURO_CRANIOTOMY_SET"],
        requires_icu_post_op=True, release_min=EMERGENCY_AT,
    )


# --------------------------------------------------------------------- page data

def _zone(verdict: AgentVerdict, limit: int = 5) -> list[dict]:
    out = []
    for item in verdict.items[:limit]:
        state = "ready" if item.ok else ("warn" if item.ready_at is not None else "busy")
        out.append({"label": item.resource, "state": state, "text": item.note})
    return out


def _zones(verdicts: list[AgentVerdict]) -> dict:
    by_agent = {v.agent: v for v in verdicts}
    return {
        "staff": _zone(by_agent[STAFFING]),
        "equip": _zone(by_agent[EQUIPMENT]),
        "or": _zone(by_agent[FACILITY]),
    }


def _blocks(result: ScheduleResult, kinds: dict[str, str] | None = None) -> list[dict]:
    kinds = kinds or {}
    out = []
    for a in result.assignments.values():
        case = result.cases[a.case_id]
        kind = kinds.get(a.case_id, "locked" if a.frozen else "booked")
        out.append({
            "case_id": a.case_id, "room": a.or_id, "start": a.start_min, "end": a.end_min,
            "clean": a.cleanup_end_min, "kind": kind, "urgency": case.urgency.value,
            "label": f"{a.case_id} {case.procedure}",
            "time": f"{fmt(a.start_min)}-{fmt(a.end_min)}",
        })
    return out


def _probe(case: SurgeryCase, room: str, start: int, kind: str, label: str) -> dict:
    return {
        "case_id": case.case_id, "room": room, "start": start, "end": start + case.duration_mins,
        "clean": start + case.duration_mins, "kind": kind, "urgency": case.urgency.value,
        "label": label, "time": f"{fmt(start)}-{fmt(start + case.duration_mins)}",
    }


def _agents(**states: tuple[str, str]) -> dict:
    base = {k: {"glow": "", "pill": p} for k, p in
            {"intake": "รอคำขอ", "master": "ศูนย์กลาง", "staff": "ว่าง", "equip": "ว่าง", "or": "ว่าง"}.items()}
    for key, (glow, pill) in states.items():
        base[key] = {"glow": glow, "pill": pill}
    return base


def _verdict_states(verdicts: list[AgentVerdict]) -> dict:
    keys = {STAFFING: "staff", EQUIPMENT: "equip", FACILITY: "or"}
    return {keys[v.agent]: ("green" if v.ok else "red", "ผ่าน" if v.ok else "ไม่ผ่าน") for v in verdicts}


def build_flow(verbose: bool = False) -> dict:
    h = build_flow_hospital()
    opt = h.optimizer(time_limit_s=10.0, num_workers=1)  # one worker: same page on every run
    booked = opt.solve_schedule(booked_cases(), "Booked schedule", verbose=verbose)

    request = new_request()
    check: SlotCheck = opt.check_slot(booked, request, REQUEST_AT, verbose=verbose)
    locked = check.result
    placed = check.assignment

    er = emergency_case()
    er_scan = scan_slot(opt, er, EMERGENCY_AT, Busy.from_assignments(locked.assignments))
    after = opt.insert_emergency(
        locked, list(locked.cases.values()), [er], now_min=EMERGENCY_AT,
        scenario_name="Emergency insert", verbose=verbose,
    )

    for name, result in (("booked", booked), ("locked", locked), ("emergency", after)):
        errors = h.validate(result)
        if errors:
            raise RuntimeError(f"{name} schedule failed validation: {errors}")

    surgeon = opt._staff_by_id[request.surgeon_id].name
    rejected = [v for v in check.verdicts if not v.ok]
    probe_room = next(r.or_id for r in h.rooms if r.supports(request.required_capabilities))
    log = check.log
    n_scan = 2 + len(check.verdicts)  # intake line, scan line, one line per agent
    scan_lines = log[2:n_scan]
    negotiation = log[n_scan:n_scan + 2] if rejected else []
    lock_lines = log[n_scan + len(negotiation):]

    steps = []
    steps.append({
        "title": "ภาพรวม: ตารางที่จองไว้แล้ว + Agent ทั้ง 5",
        "desc": (
            f"ตารางตั้งต้นมี {len(booked.assignments)} เคสใน {len(h.rooms)} ห้อง "
            f"(solver: {thai.solver(booked.status)}, {booked.wall_time_s:.2f} วินาที) ทุกค่าบนหน้านี้คำนวณจาก orchestra_engine จริง "
            "กดปุ่ม 1-5 เพื่อดูแต่ละขั้น"
        ),
        "logs": [
            f"[System] โหลดห้องผ่าตัด {len(h.rooms)} ห้อง บุคลากร {len(h.staff)} คน เครื่องมือ {len(h.equipments)} ชิ้น "
            f"เตียงพักฟื้น {len(h.beds)} เตียง",
            f"[Master Orchestrator] ตารางที่จองไว้: {len(booked.assignments)} เคส (ผล {thai.solver(booked.status)})",
            *[f"[Master Orchestrator] {r}: " + ", ".join(f"{a.case_id} {fmt(a.start_min)}-{fmt(a.end_min)}" for a in items)
              for r, items in booked.by_room().items()],
        ],
        "agents": _agents(),
        "beams": ["intake-master", "master-staff", "master-equip", "master-or"],
        "zones": _zones(check.verdicts),
        "zoneTime": fmt(REQUEST_AT),
        "blocks": _blocks(booked),
        "summary": f"สถานะ: จองแล้ว {len(booked.assignments)} เคส ไม่มีการชนกัน",
    })
    steps.append({
        "title": "Step 1: Intake & Triage รับคำขอผ่าตัด",
        "desc": (
            f"{surgeon} ขอจอง {request.procedure} ({request.case_id}) เวลา {fmt(REQUEST_AT)} "
            f"ระยะเวลา {request.duration_mins} นาที ต้องใช้ห้อง {', '.join(sorted(request.required_capabilities))} "
            f"และ {', '.join(request.required_equipment_types)} "
            "(รอบนี้คำขอเป็นข้อมูลโครงสร้าง ส่วน NLP/เสียงและการพยากรณ์เวลาจากประวัติยังไม่ได้ทำ)"
        ),
        "logs": [log[0]],
        "agents": _agents(intake=("cyan", "อ่านคำขอ"), master=("gold", "รับเคส")),
        "beams": ["intake-master"],
        "zones": _zones(check.verdicts),
        "zoneTime": fmt(REQUEST_AT),
        "blocks": _blocks(booked) + [_probe(request, probe_room, REQUEST_AT, "probe", f"{request.case_id} ? {fmt(REQUEST_AT)}")],
        "summary": f"สถานะ: กำลังหาสล็อตให้ {request.case_id} ({request.duration_mins} นาที)",
    })
    steps.append({
        "title": f"Step 2: สแกนทรัพยากรพร้อมกันที่ {fmt(REQUEST_AT)}",
        "desc": (
            "Master Orchestrator ส่งเวลาเป้าหมายให้ Agent ทั้ง 3 ตรวจพร้อมกัน: "
            + "; ".join(f"{v.agent} {'ผ่าน' if v.ok else 'ไม่ผ่าน'}" for v in check.verdicts)
        ),
        "logs": [log[1], *scan_lines],
        "agents": _agents(master=("gold", "สแกนพร้อมกัน"), **_verdict_states(check.verdicts)),
        "beams": ["master-staff", "master-equip", "master-or", "staff-zone", "equip-zone", "or-zone"],
        "zones": _zones(check.verdicts),
        "zoneTime": fmt(REQUEST_AT),
        "blocks": _blocks(booked) + [_probe(
            request, probe_room, REQUEST_AT, "conflict" if rejected else "probe-ok",
            f"{request.case_id} {fmt(REQUEST_AT)} " + ("ชนเงื่อนไข" if rejected else "ผ่าน"),
        )],
        "summary": (f"สถานะ: พบข้อขัดแย้ง {len(rejected)} Agent ที่ {fmt(REQUEST_AT)}" if rejected
                    else f"สถานะ: ทรัพยากรพร้อมที่ {fmt(REQUEST_AT)}"),
    })
    ready_hints = ", ".join(f"{v.agent} พร้อม {fmt(v.ready_at)}" if v.ready_at is not None else f"{v.agent} ไม่ว่างวันนี้"
                            for v in rejected)
    steps.append({
        "title": "Step 3: เจรจาและหาช่วงเวลาใหม่ (CP-SAT)",
        "desc": (
            (f"Agent ที่ปฏิเสธแจ้งเวลาที่ตัวเองพร้อม ({ready_hints}) แล้ว Master ให้ CP-SAT หาช่วงแรกที่ทุก Agent ผ่านพร้อมกัน "
             f"ได้ {placed.or_id} {fmt(placed.start_min)}-{fmt(placed.end_min)}") if rejected and placed else
            "ทุก Agent ผ่านตั้งแต่รอบแรก จึงไม่ต้องเจรจา" if placed else
            "ไม่มีช่วงเวลาที่ทุก Agent ผ่านในวันนี้"
        ),
        "logs": negotiation + lock_lines[:1],
        "agents": _agents(master=("gold", "กำลังคำนวณ"), **{
            {STAFFING: "staff", EQUIPMENT: "equip", FACILITY: "or"}[v.agent]:
                ("purple", f"พร้อม {fmt(v.ready_at)}" if v.ready_at is not None else "วันนี้ไม่ว่าง")
            for v in rejected
        }),
        "beams": [f"master-{k}" for k in ("staff", "equip", "or")
                  if any({STAFFING: "staff", EQUIPMENT: "equip", FACILITY: "or"}[v.agent] == k for v in rejected)],
        "zones": _zones(check.verdicts),
        "zoneTime": fmt(REQUEST_AT),
        "blocks": _blocks(booked) + ([_probe(request, placed.or_id, placed.start_min, "moving",
                                             f"{request.case_id} -> {fmt(placed.start_min)}")] if placed else []),
        "summary": f"สถานะ: เสนอ {fmt(placed.start_min)}-{fmt(placed.end_min)}" if placed else "สถานะ: ไม่มีสล็อต",
    })
    locked_scan = scan_slot(opt, request, placed.start_min, Busy.from_assignments(booked.assignments))
    steps.append({
        "title": "Step 4: ล็อกตารางสำเร็จ",
        "desc": (
            f"{request.case_id} ถูกล็อกที่ {placed.or_id} {fmt(placed.start_min)}-{fmt(placed.end_min)} "
            f"(ทำความสะอาดเสร็จ {fmt(placed.cleanup_end_min)}) จองเครื่องมือ {', '.join(placed.equipment_unit_ids)} "
            f"และเตียง {placed.bed_id} โดยไม่ขยับเคสเดิม ตัวตรวจอิสระยืนยันว่าไม่มีการชนกัน"
        ),
        "logs": lock_lines,
        "agents": _agents(master=("green", "ล็อกตารางแล้ว"), staff=("green", "แจ้งทีมแล้ว"),
                          equip=("green", "จองเครื่องมือแล้ว"), **{"or": ("green", f"จอง {placed.or_id} แล้ว")}),
        "beams": ["master-staff", "master-equip", "master-or", "staff-zone", "equip-zone", "or-zone"],
        "zones": _zones(locked_scan),
        "zoneTime": fmt(placed.start_min),
        "blocks": _blocks(locked, {request.case_id: "new"}),
        "summary": f"สถานะ: ล็อก {request.case_id} สำเร็จ ไม่มีทรัพยากรชนกัน",
    })
    er_placed = after.assignments.get(er.case_id)
    moved = [c for c in after.changes if c.case_id != er.case_id]
    kinds = {c.case_id: "moved" for c in moved}
    kinds[er.case_id] = "emergency"
    er_name = opt._staff_by_id[er.surgeon_id].name
    people = sorted({n.staff_id for n in after.notifications})
    steps.append({
        "title": f"Step 5: เคสฉุกเฉินเข้ามา {fmt(EMERGENCY_AT)} (Ripple Effect)",
        "desc": (
            f"{er.procedure} โดย {er_name} "
            + (f"ได้ห้อง {er_placed.or_id} เวลา {fmt(er_placed.start_min)} " if er_placed else "ยังหาห้องไม่ได้ ")
            + "เคสที่เริ่มไปแล้วถูกล็อกไว้ "
            + (f"เลื่อน {len(moved)} เคส: " + "; ".join(f"{c.case_id} {', '.join(c.details)}" for c in moved)
               if moved else "ไม่ต้องเลื่อนเคสอื่น")
            + f" และแจ้งเตือน {len(people)} คน"
        ),
        "logs": [
            f"[Intake & Triage Agent] ฉุกเฉิน {er.case_id} {er.procedure} | {er_name} | {er.duration_mins} นาที | "
            f"มาถึง {fmt(EMERGENCY_AT)}",
            *[f"[{v.agent}] {'ผ่าน' if v.ok else 'ไม่ผ่าน'} เวลา {fmt(EMERGENCY_AT)}: {v.summary()}" for v in er_scan],
            "[Master Orchestrator] ล็อกเคสที่เริ่มไปแล้ว แล้วจัดเคสที่เหลือใหม่ (ripple effect)",
            *[f"[Master Orchestrator] {c.case_id}: {'; '.join(c.details)}" for c in after.changes],
            *([f"[Master Orchestrator] {er.case_id}: {'; '.join(er_placed.reasoning)}"] if er_placed else []),
            f"[Staffing Agent] แจ้งเตือน {len(after.notifications)} ข้อความ ถึง {len(people)} คน",
        ],
        "agents": _agents(intake=("red", "ฉุกเฉิน"), master=("red", "จัดตารางใหม่อัตโนมัติ"),
                          staff=("orange", "เรียกทีมเวร"), **{"or": ("red", "เคลียร์ห้อง")}),
        "beams": ["intake-master", "master-staff", "master-or", "or-zone", "staff-zone"],
        "zones": _zones(er_scan),
        "zoneTime": fmt(EMERGENCY_AT),
        "blocks": _blocks(after, kinds),
        "now": EMERGENCY_AT,
        "summary": f"สถานะ: แทรกเคสฉุกเฉิน + เลื่อน {len(moved)} เคสอัตโนมัติ",
    })

    all_blocks = [b for s in steps for b in s["blocks"]]
    start = min(b["start"] for b in all_blocks) // 60 * 60
    end = -(-max(b["clean"] for b in all_blocks) // 60) * 60
    return {
        "generatedBy": "python -m orchestra_engine.flow",
        "rooms": [{"id": r.or_id, "type": r.room_type, "caps": sorted(r.capabilities)} for r in h.rooms],
        "axis": {"start": start, "end": end},
        "steps": steps,
        "results": {"booked": booked.to_dict(), "slotCheck": check.to_dict(), "emergency": after.to_dict()},
    }


def write_page(data: dict, page: Path = PAGE) -> None:
    html = page.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, indent=1).replace("</", "<\\/")
    new, n = _DATA_BLOCK.subn(lambda m: m.group(1) + "\n" + payload + "\n" + m.group(3), html, count=1)
    if n != 1:
        raise RuntimeError(f"no flow-data block found in {page}")
    page.write_text(new, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--print", action="store_true", help="print the agents' log instead of writing the page")
    parser.add_argument("--page", type=Path, default=PAGE)
    args = parser.parse_args()
    data = build_flow(verbose=args.print)
    if args.print:
        for step in data["steps"]:
            print(f"## {step['title']}\n" + "\n".join(step["logs"]) + "\n")
        return
    write_page(data, args.page)
    print(f"updated {args.page}")


if __name__ == "__main__":
    main()
