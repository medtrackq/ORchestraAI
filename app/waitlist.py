"""Waiting list (due dates and order) and the search for the first free days across several days.

Shared by the Supabase store and the demo store; nothing here touches the database.
"""
from __future__ import annotations

from datetime import date, timedelta

from orchestra_engine import fmt

from .store import StoreError, with_roles

DUE_SOON_DAYS = 7      # "ใกล้ครบกำหนด": due within a week
SEARCH_DAYS = 14       # default span of the free-day search
MAX_SEARCH_DAYS = 28
SEARCH_SECONDS = 40.0  # stop and offer to carry on rather than keep the browser waiting
SEARCH_SOLVE_S = 4.0   # time limit of one CP-SAT solve during the search
WANT = 3               # days to offer
THAI_DAYS = ("จ.", "อ.", "พ.", "พฤ.", "ศ.", "ส.", "อา.")
THAI_MONTHS = ("ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค.")


def thai_date(day: date) -> str:
    """5 พ.ย. 2569"""
    return f"{day.day} {THAI_MONTHS[day.month - 1]} {day.year + 543}"


def parse_due(text, today: date | None = None) -> date | None:
    """The date a waiting case should be operated by (YYYY-MM-DD), or None."""
    if text in (None, ""):
        return None
    try:
        due = text if isinstance(text, date) else date.fromisoformat(str(text))
    except ValueError as exc:
        raise StoreError(f"วันที่ควรผ่าตัดไม่ถูกต้อง: {text!r} (ใช้ YYYY-MM-DD)") from exc
    today = today or date.today()
    if not today - timedelta(days=366) <= due <= today + timedelta(days=731):
        raise StoreError("วันที่ควรผ่าตัดต้องอยู่ภายใน 2 ปีนับจากวันนี้")
    return due


def due_info(due: date | None, today: date) -> dict:
    """Days left and whether the case is overdue / due soon (for the waiting list)."""
    if due is None:
        return {"due_date": None, "days_left": None, "due_status": None}
    left = (due - today).days
    status = "overdue" if left < 0 else "soon" if left <= DUE_SOON_DAYS else "ok"
    return {"due_date": due.isoformat(), "days_left": left, "due_status": status}


RANK = {"EMERGENCY": 0, "URGENT": 1, "ELECTIVE": 2}


def order_key(row: dict):
    """Emergency, then urgent, then by due date (no due date last), then the longest wait."""
    return (RANK.get(row["urgency"], 3), row.get("due_date") or "9999-12-31", -(row.get("waiting_days") or 0),
            row.get("code") or "")


def search_days(first: date, days: int, skip_weekends: bool) -> list[date]:
    days = max(1, min(int(days), MAX_SEARCH_DAYS))
    span = [first + timedelta(days=i) for i in range(days)]
    return [d for d in span if not (skip_weekends and d.weekday() >= 5)]


def search_start(requested: int, now_min: int) -> int | None:
    """Earliest minute to ask for: the requested time, or now (rounded up to 5 minutes) on the day itself."""
    start = max(requested, -(-now_min // 5) * 5)
    return start if start < 24 * 60 else None


def found_row(day: date | None, check, hospital, requested: int, due: date | None) -> dict | None:
    """One offered day from a ``check_slot`` result (None when the case did not fit)."""
    placed = check.assignment
    if placed is None:
        return None
    booking = next(b for b in with_roles(check.result.to_dict(), hospital)["bookings"] if b["case_id"] == check.case.case_id)
    return {
        "date": day.isoformat() if day else None,
        "label": f"{THAI_DAYS[day.weekday()]} {thai_date(day)}" if day else "วันตัวอย่าง",
        "start": fmt(placed.start_min), "end": fmt(placed.end_min), "start_min": placed.start_min,
        "or_id": placed.or_id, "bed_id": placed.bed_id, "as_requested": placed.start_min == requested,
        "team": [{"name": t["name"], "role": t["role"]} for t in booking.get("team", [])],
        "after_due": bool(due and day and day > due),
    }


def not_found(day: date | None, check) -> dict:
    reasons = check.result.unscheduled.get(check.case.case_id) or ["ไม่มีช่วงที่ทุก Agent ผ่านพร้อมกัน"]
    return {"date": day.isoformat() if day else None, "label": thai_date(day) if day else "วันตัวอย่าง",
            "reason": "; ".join(reasons[:2])}


def alerts(rows: list[dict]) -> list[dict]:
    """Waiting cases past or near the date they should be operated by, most urgent first."""
    due = [r for r in rows if r.get("due_status") in ("overdue", "soon")]
    return sorted(due, key=lambda r: (r["days_left"], r.get("code") or ""))


def digest_event(day: str, rows: list[dict]) -> dict:
    """The daily reminder of waiting cases past or near their due date (case codes only, no patient identity)."""
    return {"type": "waitlist_digest", "day": day, "cases": [
        {"code": r["code"], "procedure": r["procedure"], "surgeon": r.get("surgeon"), "surgeon_id": r.get("surgeon_id"),
         "status": r["due_status"], "days_left": r["days_left"], "due_date": r["due_date"]} for r in alerts(rows)]}
