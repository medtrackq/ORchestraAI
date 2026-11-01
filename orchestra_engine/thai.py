"""Thai words shared by the agents' messages (the people reading them are Thai OR staff).

Codes stay as they are (room, unit, bed and supply codes, equipment types,
procedure names); only the words around them are Thai. Agent names
(Staffing Agent, Master Orchestrator, ...) are names and stay in English.
"""
from __future__ import annotations

ROLE = {
    "SURGEON": "ศัลยแพทย์",
    "ANESTHESIOLOGIST": "วิสัญญีแพทย์",
    "SCRUB_NURSE": "พยาบาลส่งเครื่องมือ",
    "CIRCULATING_NURSE": "พยาบาลช่วยรอบนอก",
}
URGENCY = {"ELECTIVE": "นัดล่วงหน้า", "URGENT": "ด่วน", "EMERGENCY": "ฉุกเฉิน"}
STATUS = {  # rooms, equipment units and recovery beds
    "AVAILABLE": "ใช้งานได้",
    "IN_USE": "กำลังใช้งาน",
    "STERILIZING": "อยู่ระหว่างทำให้ปราศจากเชื้อที่ CSSD",
    "MAINTENANCE": "ซ่อมบำรุง",
    "CLOSED": "ปิดใช้งาน",
    "OUT_OF_SERVICE": "งดใช้งาน",
    "OCCUPIED": "มีผู้ป่วยอยู่",
    "CLEANING": "กำลังทำความสะอาด",
}
SOLVER = {"OPTIMAL": "ดีที่สุด", "FEASIBLE": "ใช้ได้", "INFEASIBLE": "จัดไม่ได้", "MODEL_INVALID": "ข้อมูลไม่ถูกต้อง",
          "UNKNOWN": "หาคำตอบไม่ทันเวลา", "EMPTY": "ไม่มีเคส"}
OTHER_DAY = "เคสของวันอื่น"
# Marks a fatigue override in the activity log; reports count it (and the
# English mark that older log lines carry).
FATIGUE_OVERRIDE = "ข้ามเกณฑ์ความเหนื่อยล้า"
FATIGUE_OVERRIDE_OLD = "FATIGUE OVERRIDE"


def role(code: str) -> str:
    return ROLE.get(code, code.replace("_", " ").title())


def urgency(value: str) -> str:
    return URGENCY.get(value, value)


def status(code: str | None) -> str:
    return STATUS.get(code or "", (code or "").lower() or "ใช้ไม่ได้")


def solver(name: str) -> str:
    return SOLVER.get(name, name)


def hours(minutes: float) -> str:
    return f"{minutes / 60:.1f} ชม."


def limit_hours(h: float) -> str:
    return f"{h:g} ชม."
