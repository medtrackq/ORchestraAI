"""Management report figures, checked by hand on a small day."""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.reports import CaseRecord, RoomInfo, build_report, error_bin, previous_period

TZ = ZoneInfo("Asia/Bangkok")
DAY = date(2026, 11, 2)


def t(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=TZ)


def rec(cid, room, urgency, start, end, cleanup, a0=None, a1=None, team=("Nurse A",), surgeon="Dr.X", proc="Lap chole"):
    return CaseRecord(cid, room, surgeon, proc, urgency, t(start), t(end), t(cleanup),
                      t(a0) if a0 else None, t(a1) if a1 else None, list(team))


CASES = [
    rec("A", "OR-1", "ELECTIVE", "08:00", "10:00", "10:30", "08:10", "10:20"),
    rec("B", "OR-1", "ELECTIVE", "10:30", "12:00", "12:30", "10:40", "12:40", proc="Hernia"),
    rec("C", "OR-2", "URGENT", "09:00", "10:00", "10:30", "09:30", "10:00", surgeon="Dr.Y"),
    rec("D", "OR-2", "EMERGENCY", "17:00", "18:00", "18:30"),
]
ROOMS = [RoomInfo("OR-1", time(8), time(16)), RoomInfo("OR-2")]


def test_report_figures():
    r = build_report(CASES, ROOMS, DAY, DAY, TZ)
    k = r["kpi"]
    assert (r["operating_days"], r["cases"], r["cases_timed"]) == (1, 4, 3)
    rooms = {x["room"]: x for x in r["rooms"]}
    # OR-1: 08:10-10:50 (incl. 30 min cleaning) + 10:40-13:10 = 310 of 480 min.
    assert rooms["OR-1"]["utilization"] == 64.6 and rooms["OR-1"]["used_hours"] == 5.2
    # OR-2 has no elective hours set: 08:00-16:00 is assumed; the 17:00 emergency is after hours.
    assert rooms["OR-2"]["utilization"] == 12.5 and rooms["OR-2"]["after_hours"] == 1.0 and not rooms["OR-2"]["hours_set"]
    assert k["utilization"] == 38.5 and k["after_hours"] == 1.0
    assert (k["on_time_pct"], k["first_cases_measured"], k["avg_first_case_delay"]) == (50.0, 2, 20.0)
    assert (k["turnover_avg"], k["turnover_planned"], k["turnovers_measured"]) == (20.0, 30.0, 1)
    # Errors: A +10, B +30, C -30.
    assert (k["duration_n"], k["duration_mae"], k["duration_bias"]) == (3, 23.3, 3.3)
    assert (k["duration_within_pct"], k["duration_overrun_pct"]) == (33.3, 33.3)
    assert [b["count"] for b in r["error_bins"]] == [0, 1, 1, 1, 0]
    assert r["mix"] == {"ELECTIVE": 2, "URGENT": 1, "EMERGENCY": 1}
    assert r["per_day"] == [{"date": "2026-11-02", "elective": 2, "unplanned": 2, "utilization": 38.5}]
    assert r["by_surgeon"][0]["name"] == "Dr.Y" and r["by_surgeon"][0]["mae"] == 30.0
    assert r["staff_hours"][0] == {"name": "Nurse A", "hours": 5.7}  # 130 + 120 + 30 + 60 = 340 minutes


def test_closures_inactive_rooms_and_empty_periods():
    closed = [RoomInfo("OR-1", time(8), time(16), closures=[(t("08:00"), t("12:00"))]), RoomInfo("OR-2", active=False)]
    r = build_report(CASES[:2], closed, DAY, DAY, TZ)
    assert [x["room"] for x in r["rooms"]] == ["OR-1"] and r["rooms"][0]["available_hours"] == 4.0
    empty = build_report([], ROOMS, DAY, DAY, TZ)
    assert empty["cases"] == 0 and empty["kpi"]["utilization"] is None and empty["kpi"]["duration_mae"] is None


def test_helpers():
    assert [error_bin(e) for e in (-31, -30, -16, -15, 0, 15, 16, 30, 31)] == [0, 1, 1, 2, 2, 2, 3, 3, 4]
    assert previous_period(date(2026, 11, 1), date(2026, 11, 30)) == (date(2026, 10, 2), date(2026, 10, 31))
