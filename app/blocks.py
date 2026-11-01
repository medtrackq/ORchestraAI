"""Weekly OR block time: a room's hours on a weekday that belong to a specialty or a surgeon.

Rules (only for a new ELECTIVE case being booked; urgent and emergency cases, and
moving cases that are already booked, ignore blocks):

* a block belongs to its owner until ``release_days`` before the date, or until
  the owner gives that date back; another surgeon's elective case cannot use it
  before then;
* the owner's own block room is preferred (other rooms cost a little in the solver).

The report shows each owner's block hours, how much their own cases used, how much
others used after release, and how many dates were given back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from orchestra_engine import Urgency

OWNER_KINDS = ("SPECIALTY", "SURGEON")
DAY_TH = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]


def norm(text) -> str:
    return " ".join(str(text or "").split()).lower()


@dataclass
class DayBlock:
    """One block on one date."""

    id: str
    room: str
    start_min: int
    end_min: int
    owner_kind: str
    specialty: str | None
    surgeon_id: str | None
    owner: str  # label: the specialty or the surgeon's name
    release_days: int
    released: bool = False
    released_why: str | None = None
    note: str | None = None
    extra: dict = field(default_factory=dict)

    def owns(self, surgeon_id: str | None, specialty: str | None) -> bool:
        if self.owner_kind == "SURGEON":
            return bool(surgeon_id) and surgeon_id == self.surgeon_id
        return bool(specialty) and norm(specialty) == norm(self.specialty)

    def to_dict(self) -> dict:
        from orchestra_engine import fmt

        return {"id": self.id, "room": self.room, "start_min": self.start_min, "end_min": self.end_min,
                "start": fmt(self.start_min), "end": fmt(self.end_min), "owner_kind": self.owner_kind,
                "owner": self.owner, "specialty": self.specialty, "surgeon_id": self.surgeon_id,
                "release_days": self.release_days, "released": self.released, "released_why": self.released_why,
                "note": self.note}


def release_state(block_id: str, release_days: int, day: date, today: date, given_back: set[str]) -> tuple[bool, str | None]:
    if block_id in given_back:
        return True, "เจ้าของคืนบล็อกวันนี้แล้ว"
    if (day - today).days <= release_days:
        return True, f"เปิดให้ทุกคนแล้ว (ภายใน {release_days} วันก่อนผ่าตัด)"
    return False, None


def case_limits(blocks: list[DayBlock], urgency, surgeon_id: str | None, specialty: str | None
                ) -> tuple[dict[str, list[tuple[int, int, str]]], tuple[str, ...]]:
    """(room -> spans the case may not use, rooms of the surgeon's own blocks) for a new case."""
    if Urgency(urgency) is not Urgency.ELECTIVE:
        return {}, ()
    held: dict[str, list[tuple[int, int, str]]] = {}
    own: list[str] = []
    for b in blocks:
        if b.owns(surgeon_id, specialty):
            own.append(b.room)
        elif not b.released:
            held.setdefault(b.room, []).append(
                (b.start_min, b.end_min, f"บล็อกของ {b.owner} (เปิดให้คนอื่น {b.release_days} วันก่อนผ่าตัด)"))
    return held, tuple(dict.fromkeys(own))


def occurrences(blocks: list[dict], first: date, last: date) -> list[dict]:
    """Each block on each date of the period it is valid on (``blocks``: rows with weekday, valid_from/to)."""
    out, day = [], first
    while day <= last:
        for b in blocks:
            if b["weekday"] == day.weekday() and (b["valid_from"] is None or b["valid_from"] <= day) \
                    and (b["valid_to"] is None or b["valid_to"] >= day):
                out.append({**b, "day": day})
        day += timedelta(days=1)
    return out


def usage(occ: list[dict], cases: list[dict], tz) -> list[dict]:
    """Report rows per owner. ``occ``: occurrences with room, day, start/end (time), owner fields and
    ``given_back``; ``cases``: room, start / end (aware datetimes, actual when recorded), surgeon_id, specialty."""
    rows: dict[str, dict] = {}
    by_room: dict[str, list[dict]] = {}
    for c in cases:
        by_room.setdefault(c["room"], []).append(c)
    for o in occ:
        w0 = datetime.combine(o["day"], o["start_time"], tzinfo=tz)
        w1 = datetime.combine(o["day"], o["end_time"], tzinfo=tz)
        block = DayBlock(str(o["block_id"]), o["room"], 0, 0, o["owner_kind"], o.get("specialty"),
                         str(o["surgeon_id"]) if o.get("surgeon_id") else None, o["owner"], o["release_days"])
        row = rows.setdefault(o["owner"], {"owner": o["owner"], "kind": o["owner_kind"], "blocks": 0, "minutes": 0.0,
                                           "own": 0.0, "others": 0.0, "given_back": 0})
        row["blocks"] += 1
        row["minutes"] += (w1 - w0).total_seconds() / 60
        row["given_back"] += 1 if o.get("given_back") else 0
        for c in by_room.get(o["room"], []):
            overlap = (min(c["end"], w1) - max(c["start"], w0)).total_seconds() / 60
            if overlap > 0:
                row["own" if block.owns(c.get("surgeon_id"), c.get("specialty")) else "others"] += overlap
    out = []
    for r in rows.values():
        out.append({"owner": r["owner"], "kind": r["kind"], "blocks": r["blocks"], "hours": round(r["minutes"] / 60, 1),
                    "own_hours": round(r["own"] / 60, 1), "others_hours": round(r["others"] / 60, 1),
                    "utilization": round(100 * r["own"] / r["minutes"], 1) if r["minutes"] else None,
                    "given_back": r["given_back"]})
    return sorted(out, key=lambda r: (-r["hours"], r["owner"]))
