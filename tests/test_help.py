"""The user manual (/help) names real buttons and menus, covers every role, and opens before login."""
from __future__ import annotations

import html
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import ROLES
from app.main import app

ROOT = Path(__file__).resolve().parent.parent / "app"
HELP = (ROOT / "static" / "help.html").read_text(encoding="utf-8")


def _labels() -> list[str]:
    """Every <kbd> label in the manual except those marked dyn (built on screen from data)."""
    out = []
    for attrs, text in re.findall(r"<kbd([^>]*)>(.*?)</kbd>", HELP, re.S):
        if "dyn" in attrs:
            continue
        out.append(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip())
    return out


def test_every_button_named_in_the_manual_exists():
    pages = [p for p in (ROOT / "static").glob("*.html") if p.name != "help.html"] + [ROOT / "static" / "feedback.js"]
    source = "\n".join(p.read_text(encoding="utf-8") for p in pages)
    source += "\n".join(p.read_text(encoding="utf-8") for p in ROOT.glob("*.py"))
    source = html.unescape(re.sub(r"\s+", " ", source))
    labels = _labels()
    assert len(labels) > 60
    missing = sorted({label for label in labels if label not in source})
    assert missing == [], f"rename these in help.html (or the page): {missing}"


def test_every_role_has_a_guide():
    sections = set(re.findall(r'<section class="guide" id="(\w+)" data-guide="\1"', HELP))
    assert set(ROLES) <= sections and {"start", "faq"} <= sections
    tabs = set(re.findall(r'<button type="button" role="tab" data-guide="(\w+)"', HELP))
    assert tabs == sections


def test_the_manual_opens_before_login_and_is_linked():
    c = TestClient(app, base_url="https://testserver")
    r = c.get("/help", follow_redirects=False)
    assert r.status_code == 200 and "คู่มือผู้จัดตาราง OR" in r.text
    for page in ("index.html", "admin.html", "status.html", "login.html"):
        assert 'href="/help' in (ROOT / "static" / page).read_text(encoding="utf-8"), page
