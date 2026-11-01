"""The steps of a case on the day: what the status board shows and the report times.

Steps, in order: arrived in pre-op holding, into the OR, incision, closure, out of
the OR (to recovery), out of recovery (back to the ward / ICU). Into and out of the
OR are the case's actual start / end (``scheduled_surgeries.actual_*_time``), which
the reports and the duration forecast already use; the other steps are kept in
``case_milestones`` (migration 011).

A case's stage is its latest recorded step; before any, it is "scheduled", or
"late" once its planned start is ``LATE_AFTER`` minutes past.
"""
from __future__ import annotations

from statistics import mean, median

from orchestra_engine import fmt

# key, label, short label, the stage shown on the board once the step is recorded, phase (colour)
STEPS = (
    ("arrived", "มาถึงห้องเตรียมผ่าตัด", "ห้องเตรียม", "รอเข้าห้องผ่าตัด", "waiting"),
    ("in_room", "เข้าห้องผ่าตัด", "เข้าห้อง", "อยู่ในห้องผ่าตัด", "in_or"),
    ("incision", "ลงมีด", "ลงมีด", "กำลังผ่าตัด", "in_or"),
    ("closure", "เย็บแผลเสร็จ", "เย็บเสร็จ", "เย็บแผลเสร็จ เตรียมออกจากห้อง", "in_or"),
    ("out_room", "ออกจากห้องผ่าตัด (ไปห้องพักฟื้น)", "ออกห้อง", "อยู่ห้องพักฟื้น", "recovery"),
    ("recovery_out", "ออกจากห้องพักฟื้น (กลับหอ/ICU)", "กลับหอ", "เสร็จสิ้น", "done"),
)
KEYS = [s[0] for s in STEPS]
LABELS = {s[0]: s[1] for s in STEPS}
STAGES = {"scheduled": ("นัดไว้", "scheduled"), "late": ("ยังไม่เข้าห้อง (ช้ากว่านัด)", "late"),
          **{s[0]: (s[3], s[4]) for s in STEPS}}
IN_TABLE = ("arrived", "incision", "closure", "recovery_out")  # the others are the case's actual times
LATE_AFTER = 15  # minutes after the planned start without being in the room
SEGMENTS = (  # report: average time between steps
    ("arrived", "in_room", "ห้องเตรียม → เข้าห้องผ่าตัด"),
    ("in_room", "incision", "เข้าห้อง → ลงมีด (เตรียมและระงับความรู้สึก)"),
    ("incision", "closure", "ลงมีด → เย็บแผลเสร็จ (เวลาผ่าตัด)"),
    ("closure", "out_room", "เย็บแผลเสร็จ → ออกจากห้อง"),
    ("in_room", "out_room", "อยู่ในห้องผ่าตัดทั้งหมด"),
    ("out_room", "recovery_out", "อยู่ห้องพักฟื้น"),
)


def summarize(minutes: dict, planned_start: int, now_min: int | None, *, late: bool = True) -> dict:
    """``minutes``: step -> minute from the day's midnight (None: not yet); ``late``: flag cases past their start."""
    recorded = [k for k in KEYS if minutes.get(k) is not None]
    last = recorded[-1] if recorded else None
    if last is not None:
        stage = last
    elif late and now_min is not None and now_min > planned_start + LATE_AFTER:
        stage = "late"
    else:
        stage = "scheduled"
    label, phase = STAGES[stage]
    after = KEYS[KEYS.index(last) + 1:] if last else KEYS
    since = minutes.get(last) if last else None
    return {
        "times": {k: fmt(minutes[k]) if minutes.get(k) is not None else None for k in KEYS},
        "stage": stage, "label": label, "phase": phase, "next": after[0] if after else None,
        "since": fmt(since) if since is not None else None,
        "elapsed": max(0, now_min - since) if since is not None and now_min is not None and stage != "recovery_out" else None,
        "late_min": now_min - planned_start if stage == "late" else None,
    }


def parse_update(data: dict) -> tuple[str, str | None, bool]:
    from .store import StoreError

    step = data.get("step")
    if step not in LABELS:
        raise StoreError(f"ขั้นตอนไม่ถูกต้อง: {step}")
    time_text = (data.get("time") or "").strip() or None
    return step, time_text, bool(data.get("clear"))


def place(minutes: dict, step: str, minute: int) -> int:
    """The minute for a typed clock time: after midnight when it follows a late-evening step."""
    before = [minutes[k] for k in KEYS[:KEYS.index(step)] if minutes.get(k) is not None]
    if before and minute < max(before) - 6 * 60:
        minute += 24 * 60
    return minute


def check(minutes: dict, step: str, minute: int | None, *, clear: bool = False) -> None:
    """Steps stay in order; only the latest step can be cleared."""
    from .store import StoreError

    i = KEYS.index(step)
    later = [k for k in KEYS[i + 1:] if minutes.get(k) is not None]
    if clear:
        if later:
            raise StoreError(f"ลบได้เฉพาะขั้นล่าสุด (มี {LABELS[later[-1]]} บันทึกไว้แล้ว)")
        return
    for k in KEYS[:i]:
        if minutes.get(k) is not None and minutes[k] > minute:
            raise StoreError(f"{LABELS[step]} ต้องไม่ก่อน {LABELS[k]} ({fmt(minutes[k])})")
    for k in later:
        if minutes[k] < minute:
            raise StoreError(f"{LABELS[step]} ต้องไม่หลัง {LABELS[k]} ({fmt(minutes[k])})")
    if step == "out_room" and minutes.get("in_room") is None:
        raise StoreError("บันทึกเวลาเข้าห้องผ่าตัดก่อน")


def segment_stats(cases: list[dict]) -> list[dict]:
    """Report rows: for each segment, how many cases have both steps and the mean / median minutes."""
    out = []
    for a, b, label in SEGMENTS:
        spans = [c[b] - c[a] for c in cases if c.get(a) is not None and c.get(b) is not None and c[b] >= c[a]]
        out.append({"from": a, "to": b, "label": label, "n": len(spans),
                    "mean": round(mean(spans), 1) if spans else None, "median": median(spans) if spans else None})
    return out
