"""Independent checker for hard constraints.

It re-checks a finished schedule without using any solver code, so tests (and
the data layer before writing to the DB) can confirm the schedule has no
conflicts. Cases marked ``frozen`` were already running when the schedule was
re-optimised, so the shift, opening-hour, release and availability checks are
skipped for them; they still count for every overlap check. Fatigue limits
(``fatigue``) are errors only for cases that are not EMERGENCY overrides.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable, Mapping

from . import thai
from .fatigue import fatigue_issues
from .models import EquipmentUnit, OperatingRoom, RecoveryBed, StaffMember, SupplyItem, SurgeryCase
from .results import Assignment, ScheduleResult
from .timeutil import MINUTES_PER_DAY, fmt


def validate_schedule(
    schedule: ScheduleResult | Mapping[str, Assignment],
    cases: Iterable[SurgeryCase],
    rooms: Iterable[OperatingRoom],
    equipments: Iterable[EquipmentUnit],
    staff: Iterable[StaffMember],
    beds: Iterable[RecoveryBed] = (),
    *,
    horizon_mins: int = MINUTES_PER_DAY,
    supplies: Iterable[SupplyItem] = (),
) -> list[str]:
    """Return a list of hard-constraint violations (empty = valid)."""
    assignments = schedule.assignments if isinstance(schedule, ScheduleResult) else schedule
    case_by_id = {c.case_id: c for c in cases}
    room_by_id = {r.or_id: r for r in rooms}
    unit_by_id = {u.unit_id: u for u in equipments}
    staff_by_id = {s.staff_id: s for s in staff}
    bed_by_id = {b.bed_id: b for b in beds}
    errors: list[str] = []

    room_busy: dict[str, list] = defaultdict(list)
    staff_busy: dict[str, list] = defaultdict(list)
    unit_busy: dict[str, list] = defaultdict(list)
    bed_busy: dict[str, list] = defaultdict(list)

    for cid, a in assignments.items():
        case = case_by_id.get(cid)
        room = room_by_id.get(a.or_id)
        if case is None or room is None:
            errors.append(f"{cid}: ไม่พบเคสหรือห้อง {a.or_id}")
            continue
        tag = f"{cid}"
        dur = case.duration_mins
        if a.end_min - a.start_min != dur:
            errors.append(f"{tag}: ช่วงเวลาที่จอง {a.end_min - a.start_min} นาที ไม่เท่ากับเวลาผ่าตัด {dur} นาที")
        if a.cleanup_end_min != a.end_min + room.turnover_for(case):
            errors.append(f"{tag}: ไม่ได้เผื่อเวลาทำความสะอาดของ {room.or_id}")
        if not room.supports(case.required_capabilities):
            errors.append(f"{tag}: {room.or_id} ไม่มี {', '.join(sorted(case.required_capabilities - room.all_capabilities))}")

        if not a.frozen:
            if not room.is_usable:
                errors.append(f"{tag}: {room.or_id} {thai.status(room.status)}")
            if a.start_min < max(case.release_min, 0):
                errors.append(f"{tag}: เริ่ม {fmt(a.start_min)} ก่อนเวลาที่เริ่มได้ {fmt(case.release_min)}")
            if case.deadline_min is not None and a.start_min > case.deadline_min:
                errors.append(f"{tag}: เริ่ม {fmt(a.start_min)} เลยกำหนด {fmt(case.deadline_min)}")
            if a.end_min > horizon_mins:
                errors.append(f"{tag}: เสร็จหลังสิ้นสุดช่วงวางแผน")
            lo, hi = room.hours_for(case, horizon_mins)
            if a.start_min < room.open_min or a.end_min > room.close(horizon_mins):
                errors.append(f"{tag}: อยู่นอกเวลาเปิดของ {room.or_id}")
            elif a.start_min < lo or a.end_min > hi:
                errors.append(f"{tag}: อยู่นอกเวลาผ่าตัดนัดของ {room.or_id} ({fmt(lo)}-{fmt(hi)})")
            shut = room.closed_at(a.start_min, a.cleanup_end_min)
            if shut:
                why = f" ({shut[2]})" if shut[2] else ""
                errors.append(f"{tag}: {room.or_id} ปิด {fmt(shut[0])}-{fmt(shut[1])}{why}")

        # Team
        if not a.staff_ids or a.staff_ids[0] != case.surgeon_id:
            errors.append(f"{tag}: ศัลยแพทย์หลัก {case.surgeon_id} ไม่ได้อยู่ลำดับแรกของทีม")
        roles: Counter[str] = Counter()
        for sid in a.staff_ids:
            person = staff_by_id.get(sid)
            if person is None:
                errors.append(f"{tag}: ไม่พบบุคลากร {sid}")
                continue
            if sid != case.surgeon_id:
                roles[person.role] += 1
                need = case.required_staff_skills.get(person.role, frozenset())
                if not need <= person.skills:
                    errors.append(f"{tag}: {person.name} ไม่มีทักษะ {', '.join(sorted(need - person.skills))}")
            if not a.frozen:
                if not person.is_active:
                    errors.append(f"{tag}: {person.name} ปิดการใช้งานอยู่")
                if not any(s <= a.start_min and a.end_min <= e for s, e in person.shifts):
                    errors.append(f"{tag}: {person.name} ไม่อยู่เวรช่วง {fmt(a.start_min)}-{fmt(a.end_min)}")
            staff_busy[sid].append((a.start_min, a.end_min, cid))
        wanted = Counter({r: n for r, n in case.required_staff_roles.items() if n})
        if roles != wanted:
            errors.append(f"{tag}: ทีมไม่ตรงกับที่ต้องการ (มี {_count(roles, thai.role)}; ต้องการ {_count(wanted, thai.role)})")

        # Equipment
        types: Counter[str] = Counter()
        for uid in a.equipment_unit_ids:
            unit = unit_by_id.get(uid)
            if unit is None:
                errors.append(f"{tag}: ไม่พบเครื่องมือ {uid}")
                continue
            types[unit.eq_type] += 1
            if not a.frozen:
                if not unit.is_usable:
                    errors.append(f"{tag}: {uid} {thai.status(unit.status)}")
                if a.start_min < unit.available_at:
                    errors.append(f"{tag}: {uid} ยังไม่กลับจาก CSSD จนถึง {fmt(unit.available_at)}")
                shut = unit.closed_at(a.start_min, a.end_min + unit.reprocess_mins)
                if shut:
                    why = f" ({shut[2]})" if shut[2] else ""
                    errors.append(f"{tag}: {uid} มีนัดซ่อมบำรุง {fmt(shut[0])}-{fmt(shut[1])}{why}")
            unit_busy[uid].append((a.start_min, a.end_min + unit.reprocess_mins, cid))
        if types != case.equipment_counts:
            errors.append(f"{tag}: เครื่องมือไม่ตรงกับที่ต้องการ (มี {_count(types)}; ต้องการ {_count(case.equipment_counts)})")

        # Recovery bed
        if bed_by_id:
            bed = bed_by_id.get(a.bed_id) if a.bed_id else None
            if bed is None:
                errors.append(f"{tag}: ยังไม่ได้จองเตียงพักฟื้น")
            else:
                if bed.bed_type != case.recovery_bed_type:
                    errors.append(f"{tag}: เตียง {bed.bed_id} เป็น {bed.bed_type} แต่ต้องการ {case.recovery_bed_type}")
                if a.recovery_start_min != a.end_min or (
                    a.recovery_end_min != a.end_min + case.recovery_duration
                ):
                    errors.append(f"{tag}: ช่วงพักฟื้นไม่ต่อจากการผ่าตัด")
                if not a.frozen and a.end_min < bed.available_from:
                    errors.append(f"{tag}: เตียง {bed.bed_id} มีผู้ป่วยถึง {fmt(bed.available_from)}")
                if not a.frozen and not bed.is_usable:
                    errors.append(f"{tag}: เตียง {bed.bed_id} {thai.status(bed.status)}")
                bed_busy[bed.bed_id].append((a.recovery_start_min, a.recovery_end_min, cid))

        room_busy[room.or_id].append((a.start_min, a.cleanup_end_min, cid))

    # Time held by bookings outside this horizon (only for resources this schedule uses).
    for busy, lookup in ((room_busy, room_by_id), (unit_busy, unit_by_id), (bed_busy, bed_by_id)):
        for resource in list(busy):
            busy[resource] += [(a, b, thai.OTHER_DAY) for a, b in getattr(lookup.get(resource), "blocked", ())]

    for kind, busy in (("ห้อง", room_busy), ("บุคลากร", staff_busy), ("เครื่องมือ", unit_busy), ("เตียง", bed_busy)):
        for resource, spans in busy.items():
            spans.sort()
            name = staff_by_id[resource].name if busy is staff_busy and resource in staff_by_id else resource
            for (s1, e1, c1), (s2, e2, c2) in zip(spans, spans[1:]):
                if s2 < e1:
                    errors.append(
                        f"{kind} {name} ถูกจองซ้อน: {c1} {fmt(s1)}-{fmt(e1)} "
                        f"ทับ {c2} {fmt(s2)}-{fmt(e2)}"
                    )

    # Supplies and implants: what the cases of this horizon hold must be in stock.
    stock = {s.item_id: s for s in supplies}
    if stock:
        held: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # code -> [locked, free]
        for cid, a in assignments.items():
            case = case_by_id.get(cid)
            for code, qty in (case.required_supplies.items() if case else ()):
                held[code][0 if a.frozen else 1] += qty
        for code, (locked, free) in sorted(held.items()):
            item = stock.get(code)
            if item is None:
                errors.append(f"วัสดุ {code}: ไม่มีในคลัง")
            elif free > max(0, item.for_booked - locked):
                errors.append(f"วัสดุ {item.name} ({code}): จองไว้ {locked + free} แต่มีเพียง {item.for_booked}"
                              + (f"; {item.note}" if item.note else ""))

    known = {cid: a for cid, a in assignments.items() if cid in case_by_id}
    for issue in fatigue_issues(known, case_by_id, staff_by_id.values(), horizon_mins=horizon_mins):
        if issue.is_error:
            errors.append(f"เกินเกณฑ์ความเหนื่อยล้า: {issue.detail()}")
    return errors


def _count(counter: Mapping[str, int], label=str) -> str:
    return ", ".join(f"{label(k)} {n}" for k, n in sorted(counter.items()) if n) or "ไม่มี"
