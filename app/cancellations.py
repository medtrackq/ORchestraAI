"""Cancelled and postponed cases: the reasons, and what the management report shows.

A booked case can be called off ("งดผ่าตัด") with a reason: it either waits for a
new day (POSTPONED) or the request is given up (CANCELLED). A request on the
waiting list can be given up too. Cases pushed off their day by an emergency, an
overrun or a re-plan are recorded on their own (reasons ``EMERGENCY``, ``OVERRUN``,
``REPLAN``). Each event is one record, so a case postponed twice counts twice.

The report counts cancellations by the day the case was planned for: the rate is
cancellations / (cases done + cancellations), "same day" are those called off on
the day itself, and reasons are split into patient and hospital causes (the
hospital ones are the ones the hospital can prevent).
"""
from __future__ import annotations

from collections import Counter

# key, label, group (patient / hospital / other), chosen by people (False: recorded by the system)
REASONS = (
    ("PATIENT_UNFIT", "ผู้ป่วยไม่พร้อมทางการแพทย์ (อาการ ผลแล็บ ความดัน ฯลฯ)", "patient", True),
    ("NPO", "ผู้ป่วยไม่ได้งดน้ำงดอาหาร", "patient", True),
    ("DECLINED", "ผู้ป่วยปฏิเสธหรือขอเลื่อน", "patient", True),
    ("NO_SHOW", "ผู้ป่วยไม่มาตามนัด", "patient", True),
    ("NOT_NEEDED", "ไม่จำเป็นต้องผ่าตัดแล้ว / เปลี่ยนแผนการรักษา", "patient", True),
    ("NO_TIME", "ห้องหรือเวลาไม่พอ", "hospital", True),
    ("STAFF", "ศัลยแพทย์หรือทีมไม่ว่าง", "hospital", True),
    ("EQUIPMENT", "เครื่องมือ วัสดุ หรือรากเทียมไม่พร้อม", "hospital", True),
    ("BED", "ไม่มีเตียง ICU หรือหอผู้ป่วย", "hospital", True),
    ("BLOOD", "เลือดไม่พร้อม / ไม่ได้จองเลือด", "hospital", True),
    ("PAPERWORK", "เอกสาร ใบยินยอม หรือสิทธิการรักษาไม่ครบ", "hospital", True),
    ("OTHER", "อื่น ๆ (ระบุ)", "other", True),
    ("EMERGENCY", "ถูกเลื่อนเพราะเคสฉุกเฉินแทรก", "hospital", False),
    ("OVERRUN", "ถูกเลื่อนเพราะเคสก่อนหน้าเกินเวลา", "hospital", False),
    ("REPLAN", "ถูกเลื่อนเมื่อจัดตารางใหม่ (คนลา ปิดห้อง เครื่องมือ)", "hospital", False),
    ("UNSPECIFIED", "ไม่ระบุเหตุผล", "other", False),
)
LABELS = {key: label for key, label, _, _ in REASONS}
GROUPS = {key: group for key, _, group, _ in REASONS}
GROUP_TH = {"patient": "ผู้ป่วย", "hospital": "โรงพยาบาล", "other": "อื่น ๆ"}
OUTCOMES = ("POSTPONED", "CANCELLED")
AUTO = {"emergency": "EMERGENCY", "overrun": "OVERRUN", "replan": "REPLAN"}  # activity kind -> reason
# a readiness item flagged as a problem suggests a reason
FROM_READINESS = {"npo": "NPO", "labs": "PATIENT_UNFIT", "anesthesia": "PATIENT_UNFIT", "consent": "PAPERWORK",
                  "blood": "BLOOD", "site": "OTHER"}


def options() -> list[dict]:
    """The reasons people can choose (for the page)."""
    return [{"key": k, "label": label, "group": g} for k, label, g, chosen in REASONS if chosen]


def parse(data: dict, *, outcome_needed: bool = True) -> tuple[str, str | None, str]:
    from .store import StoreError

    reason = (data.get("reason") or "").upper()
    if reason not in LABELS or not dict((k, c) for k, _, _, c in REASONS)[reason]:
        raise StoreError("กรุณาเลือกเหตุผลที่งดผ่าตัด")
    note = (data.get("note") or "").strip()[:200] or None
    if reason == "OTHER" and not note:
        raise StoreError("เหตุผล 'อื่น ๆ' ต้องระบุรายละเอียด (ไม่ต้องใส่ชื่อหรือ HN ผู้ป่วย)")
    outcome = (data.get("outcome") or "POSTPONED").upper() if outcome_needed else "CANCELLED"
    if outcome not in OUTCOMES:
        raise StoreError(f"ผลไม่ถูกต้อง: {outcome}")
    return reason, note, outcome


def suggest(readiness: dict | None) -> str | None:
    """A reason that fits the case's readiness problems, if any."""
    for item in (readiness or {}).get("problems", []):
        if item in FROM_READINESS:
            return FROM_READINESS[item]
    return None


def summarize(rows: list[dict], done: int, *, waiting: int = 0) -> dict:
    """Report section. ``rows``: cancellations of cases planned in the period (dicts with
    reason, outcome, same_day, surgeon, procedure, case_code, planned, note); ``done``: cases
    done or still booked in the period; ``waiting``: waiting requests given up in the period."""
    by_reason = Counter(r["reason"] for r in rows)
    groups = Counter(GROUPS.get(r["reason"], "other") for r in rows)
    total = len(rows)
    pct = (lambda n, d: None if not d else round(100 * n / d, 1))  # noqa: E731
    by_surgeon = Counter(r["surgeon"] for r in rows if r.get("surgeon"))
    return {
        "total": total, "rate": pct(total, done + total), "scheduled": done + total,
        "same_day": sum(1 for r in rows if r["same_day"]), "same_day_rate": pct(sum(1 for r in rows if r["same_day"]),
                                                                              done + total),
        "postponed": sum(1 for r in rows if r["outcome"] == "POSTPONED"),
        "cancelled": sum(1 for r in rows if r["outcome"] == "CANCELLED"),
        "hospital": groups["hospital"], "patient": groups["patient"], "other": groups["other"],
        "hospital_pct": pct(groups["hospital"], total), "waiting_given_up": waiting,
        "by_reason": [{"reason": k, "label": LABELS.get(k, k), "group": GROUPS.get(k, "other"), "count": n}
                      for k, n in sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))],
        "by_surgeon": [{"name": n, "count": c} for n, c in by_surgeon.most_common(8)],
        "recent": [{k: r.get(k) for k in ("case_code", "planned", "procedure", "surgeon", "reason", "outcome",
                                          "same_day", "note")} | {"label": LABELS.get(r["reason"], r["reason"])}
                   for r in sorted(rows, key=lambda r: r["planned"], reverse=True)[:15]],
    }
