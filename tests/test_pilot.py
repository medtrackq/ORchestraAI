"""The pilot kit (/pilot): the plan, test steps per role and the evaluation form name only buttons that exist,
use only demo data that exists, and open before signing in (they are printed for training)."""
from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import ROLES
from app.main import app
from app.store import ScheduleStore

ROOT = Path(__file__).resolve().parent.parent / "app"
PILOT = (ROOT / "static" / "pilot.html").read_text(encoding="utf-8")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", fragment))).strip()


def test_every_button_named_in_the_kit_exists():
    pages = [p for p in (ROOT / "static").glob("*.html") if p.name not in ("help.html", "pilot.html")]
    source = "\n".join(p.read_text(encoding="utf-8") for p in pages + [ROOT / "static" / "feedback.js"])
    source += "\n".join(p.read_text(encoding="utf-8") for p in ROOT.glob("*.py"))
    source = html.unescape(re.sub(r"\s+", " ", source))
    labels = [_text(t) for attrs, t in re.findall(r"<kbd([^>]*)>(.*?)</kbd>", PILOT, re.S) if "dyn" not in attrs]
    assert len(labels) > 80
    missing = sorted({label for label in labels if label not in source})
    assert missing == [], f"rename these in pilot.html (or the page): {missing}"


def test_every_role_has_test_steps_and_the_tabs_match():
    parts = set(re.findall(r'<section class="part" id="(\w+)" data-part="\1"', PILOT))
    assert {"scheduler", "surgeon", "staff", "cssd", "admin"} <= parts and {"plan", "prep", "survey", "log"} <= parts
    assert set(ROLES) <= parts
    tabs = set(re.findall(r'<button type="button" role="tab" data-part="(\w+)"', PILOT))
    assert tabs == parts
    for role, prefix in (("scheduler", "S"), ("surgeon", "D"), ("staff", "N"), ("cssd", "C"), ("admin", "A")):
        section = re.search(rf'<section class="part" id="{role}".*?</section>', PILOT, re.S).group(0)
        steps = re.findall(rf"<td>{prefix}(\d+)</td>", section)
        assert steps and [int(s) for s in steps] == list(range(1, len(steps) + 1)), role  # numbered in order


def test_the_demo_data_the_steps_use_exists():
    demo = ScheduleStore(workers=2, time_limit_s=10)
    state = demo.state()
    options = state["options"]
    surgeons = {s["name"].split(" ")[0] for s in options["surgeons"]}
    procedures = {p["name"] for p in options["procedures"]}
    rooms = {r["id"] for r in options["rooms"]}
    text = _text(PILOT)
    for name in set(re.findall(r"Dr\.[A-Z][a-z]+", text)):
        assert name in surgeons, name
    for name in ("Laparoscopic cholecystectomy", "Laparoscopic appendectomy", "Open inguinal hernia repair",
                 "Emergency exploratory laparotomy"):
        assert name in text and name in procedures, name
    assert set(re.findall(r"OR-\d", text)) <= rooms
    assert {b["case_id"] for b in state["schedule"]["bookings"]} >= {"C01", "C10"}
    units = {u.unit_id for u in demo.hospital.equipments}  # the CSSD tab lists only sets still needed today (time of day)
    assert set(re.findall(r"\b[A-Z]{2,4}-0\d\b", text)) <= units


def test_the_kit_opens_before_signing_in_and_is_linked():
    r = TestClient(app, base_url="https://testserver").get("/pilot")
    assert r.status_code == 200 and "แผนทดลองใช้ 2 สัปดาห์" in r.text
    assert 'href="/pilot"' in (ROOT / "static" / "help.html").read_text(encoding="utf-8")
    assert not re.search(r"HN (?!TEST-)[0-9A-Z]", _text(PILOT))  # only made-up TEST- numbers, never a real-looking HN
