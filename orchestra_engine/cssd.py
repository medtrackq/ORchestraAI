"""Equipment & CSSD Agent: the CSSD work queue.

For every reusable unit that needs reprocessing, each use in the schedule
creates a job: the unit comes back when the case ends, is ready again
``reprocess_mins`` later (or when CSSD says, if CSSD reported a later time),
and is needed by the next case that uses it. The slack between ready and
needed shows which trays CSSD must do first.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Mapping

from .models import EquipmentUnit
from .results import Assignment
from .timeutil import fmt

TIGHT_MINS = 30  # less spare time than this between ready and needed is flagged


def cssd_queue(
    assignments: Mapping[str, Assignment],
    units: Iterable[EquipmentUnit],
    *,
    now_min: int = 0,
) -> list[dict]:
    """Reprocessing jobs of the day, most urgent (least slack) first."""
    uses: dict[str, list[Assignment]] = defaultdict(list)
    for a in assignments.values():
        for uid in a.equipment_unit_ids:
            uses[uid].append(a)
    jobs = []
    for u in units:
        if u.reprocess_mins <= 0:
            continue
        mine = sorted(uses.get(u.unit_id, []), key=lambda a: a.start_min)
        reported = u.available_at if u.status in ("STERILIZING", "IN_USE") and u.available_at > now_min else None
        last_done = max((i for i, a in enumerate(mine) if a.end_min <= now_min), default=None)
        if reported is not None and last_done is None:
            nxt = next((a for a in mine if a.start_min >= now_min), None)
            jobs.append(_job(u, None, reported, nxt, now_min, reported=True))
        for i, a in enumerate(mine):
            ready = a.end_min + u.reprocess_mins
            reported_here = i == last_done and reported is not None and reported > ready
            if reported_here:
                ready = reported
            nxt = mine[i + 1] if i + 1 < len(mine) else None
            if ready <= now_min and (nxt is None or nxt.start_min <= now_min):
                continue  # done and already used again: history
            jobs.append(_job(u, a, ready, nxt, now_min, reported=reported_here))
    order = {"late": 0, "tight": 1, "ok": 2, "free": 3}
    return sorted(jobs, key=lambda j: (order[j["level"]], j["ready_min"], j["unit_id"]))


def _job(u: EquipmentUnit, used: Assignment | None, ready: int, nxt: Assignment | None, now_min: int,
         *, reported: bool) -> dict:
    slack = None if nxt is None else nxt.start_min - ready
    level = "free" if slack is None else "late" if slack < 0 else "tight" if slack < TIGHT_MINS else "ok"
    if used is not None and used.end_min > now_min:
        state = "in_case"  # still in the operating room
    elif ready > now_min:
        state = "processing"
    else:
        state = "ready"
    return {
        "unit_id": u.unit_id, "type": u.eq_type, "name": u.name or u.eq_type,
        "from_case": used.case_id if used else None, "from_room": used.or_id if used else None,
        "back_min": used.end_min if used else None, "ready_min": ready, "ready": fmt(ready),
        "reprocess_mins": u.reprocess_mins, "reported": reported,
        "next_case": nxt.case_id if nxt else None, "next_room": nxt.or_id if nxt else None,
        "next_start_min": nxt.start_min if nxt else None, "slack_mins": slack,
        "level": level, "state": state, "status": u.status,
    }
