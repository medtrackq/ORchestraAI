"""Pre-operative readiness: a checklist per case, shown on the schedule and sent as reminders.

Items (``ITEMS``): surgical consent, pre-anaesthetic assessment, labs, NPO, blood
reserved (only when the procedure needs it; otherwise "not needed" by itself) and
site marking. Each item is not done yet (no record), done, not needed, or a
problem (with a note). A case is *ready* when every item is done or not needed.

Readiness never blocks booking: it warns. The schedule shows a badge per case and
banners for the day on screen and for the next day with cases; marking a problem
tells the schedulers and the case's surgeon at once, and a daily digest (default
14:00, ``READINESS_DIGEST_HOUR``) lists the next day's cases that are not ready.
Messages carry case codes, times, rooms and item names only - never notes, names
or HNs.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

log = logging.getLogger("orchestra.readiness")

# key, label, short label, needed when ("blood": only if the procedure needs blood)
ITEMS = (
    ("consent", "ใบยินยอมผ่าตัด", "ยินยอม", None),
    ("anesthesia", "ประเมินก่อนระงับความรู้สึก", "วิสัญญี", None),
    ("labs", "ผลแล็บ/ผลตรวจก่อนผ่าตัด", "แล็บ", None),
    ("npo", "งดน้ำงดอาหาร", "NPO", None),
    ("blood", "จองเลือด", "เลือด", "blood"),
    ("site", "ทำเครื่องหมายตำแหน่งผ่าตัด", "Site", None),
)
LABELS = {key: label for key, label, _, _ in ITEMS}
STATUSES = ("PENDING", "DONE", "NA", "PROBLEM")  # PENDING = no record
LOOKAHEAD_DAYS = 3  # the "next day with cases" is looked for this far ahead


def summarize(rows: dict, needs_blood: bool) -> dict:
    """Stored rows ``{item: {status, note, by, at}}`` -> the case's checklist and overall status."""
    items, pending, problems = {}, [], []
    for key, label, short, needed in ITEMS:
        row = rows.get(key)
        if row:
            status = row["status"]
        elif needed == "blood" and not needs_blood:
            status = "NA"
        else:
            status = "PENDING"
        items[key] = {"status": status, "note": (row or {}).get("note"), "by": (row or {}).get("by"),
                      "at": (row or {}).get("at"), "auto": row is None and status == "NA"}
        if status == "PENDING":
            pending.append(key)
        elif status == "PROBLEM":
            problems.append(key)
    status = "problem" if problems else "pending" if pending else "ready"
    return {"status": status, "pending": pending, "problems": problems,
            "done": len(ITEMS) - len(pending) - len(problems), "total": len(ITEMS), "items": items}


def parse_update(data: dict) -> tuple[str, str, str | None]:
    from .store import StoreError

    item = data.get("item")
    if item not in LABELS:
        raise StoreError(f"รายการไม่ถูกต้อง: {item}")
    status = (data.get("status") or "").upper()
    if status not in STATUSES:
        raise StoreError(f"สถานะไม่ถูกต้อง: {status}")
    note = (data.get("note") or "").strip()[:200] or None
    if status == "PROBLEM" and not note:
        raise StoreError("กรุณาบอกว่ามีปัญหาอะไร (ไม่ต้องใส่ชื่อหรือ HN ผู้ป่วย)")
    return item, status, note


def missing_text(summary: dict) -> str:
    """Short reminder text: what is still open (no notes)."""
    short = {key: s for key, _, s, _ in ITEMS}
    parts = [f"มีปัญหา: {', '.join(short[k] for k in summary['problems'])}"] if summary["problems"] else []
    if summary["pending"]:
        parts.append(f"ยังไม่ได้: {', '.join(short[k] for k in summary['pending'])}")
    return " · ".join(parts)


def problem_event(day, case_id: str, item: str, when: str, surgeon_id: str | None, actor: dict | None) -> dict:
    return {"type": "readiness", "day": day.isoformat() if day else None, "case": case_id, "item": LABELS[item],
            "when": when, "surgeon_id": surgeon_id, "actor_id": (actor or {}).get("user_id")}


def digest_event(next_day: dict) -> dict:
    return {"type": "readiness_digest", "day": next_day["date"],
            "cases": [{"case": c["case_id"], "when": f"{c['start']} ห้อง {c['or_id']}", "surgeon_id": c["surgeon_id"],
                       "missing": missing_text(c["readiness"])} for c in next_day["cases"]]}


class DigestTimer:
    """Once a day, at or after ``hour`` (local time of the store), send the next day's not-ready cases,
    and the waiting-list cases past or near the date they should be operated by.

    The store claims each target day once (``claim_digest``), so restarts and several
    workers do not send it twice. On a server that sleeps, it goes out on the next wake.
    """

    def __init__(self, store, send, *, hour: int = 14, every_s: float = 300.0) -> None:
        self.store, self.send, self.hour, self.every_s = store, send, hour, every_s
        self._stop = threading.Event()
        self._done: set[str] = set()  # target days this process has already handled
        self._waitlist_done: set[str] = set()  # days this process has already sent the waiting-list reminder
        self._checked = None  # the day this process last noted its check

    def start(self) -> None:
        threading.Thread(target=self._loop, name="readiness-digest", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        wait = 30.0  # let the server finish starting first
        while not self._stop.wait(wait):
            self.tick()
            wait = self.every_s

    def _heartbeat(self, now: datetime) -> None:
        """Note that today's check ran (the status page warns when a sleeping server missed it)."""
        if self._checked == now.date():
            return
        self._checked = now.date()
        from .system_status import mark_job

        mark_job(self.store, "digest", now)

    def tick(self, now: datetime | None = None) -> bool:
        """Send the digest if it is time and not sent yet; True when sent."""
        try:
            tz = getattr(getattr(self.store, "db", None), "tz", None)
            now = now or datetime.now(tz)
            if now.hour < self.hour or not hasattr(self.store, "readiness_next"):
                return False
            self._heartbeat(now)
            self.waitlist(now)
            nxt = self.store.readiness_next()
            if not nxt or not nxt["cases"] or nxt["date"] in self._done:
                return False
            self._done.add(nxt["date"])
            if not self.store.claim_digest(nxt["date"]):
                return False
            self.send(digest_event(nxt))
            return True
        except Exception:  # reminders must never take the server down
            log.exception("readiness digest failed")
            return False

    def waitlist(self, now: datetime) -> bool:
        """Once a day: waiting cases past or near their due date (nothing when there are none)."""
        if not hasattr(self.store, "waitlist_due"):
            return False
        day = now.date().isoformat()
        if day in self._waitlist_done:
            return False
        try:
            from .waitlist import digest_event as waitlist_event

            rows = self.store.waitlist_due()
            self._waitlist_done.add(day)
            if not rows or not self.store.claim_digest(day, kind="waitlist"):
                return False
            self.send(waitlist_event(day, rows))
            return True
        except Exception:  # reminders must never take the server down
            log.exception("waiting list reminder failed")
            return False


def digest_hour() -> int | None:
    """``READINESS_DIGEST_HOUR`` (0-23, default 14); "off" turns the digest off."""
    raw = (os.environ.get("READINESS_DIGEST_HOUR") or "14").strip().lower()
    if raw in ("off", "none", "-1", ""):
        return None
    try:
        return min(23, max(0, int(raw)))
    except ValueError:
        return 14
