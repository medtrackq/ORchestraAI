"""Which system this is: the real one, or a practice system (``APP_ENVIRONMENT=staging``).

A practice system looks exactly like the real one, so people trained on it could book a real patient there,
or try things on the real one by mistake. On a practice system:

* every page carries an orange strip "ระบบซ้อม · ข้อมูลไม่จริง" (added by the server, so no page can miss it,
  on screen and on paper) and its tab title starts with "[ซ้อม]";
* notifications (phone, LINE) start with "[ซ้อม]";
* the installed app is called "ORchestraAI (ซ้อม)";
* an admin can start over with fresh demo data before each training round (``reset_practice``).
"""
from __future__ import annotations

import json
import os
import re

PRACTICE_NAMES = {"staging", "training", "practice", "test", "ซ้อม"}
PREFIX = "[ซ้อม] "
RESET_PHRASE = "เริ่มระบบซ้อมใหม่"
# A strip at the top (scrolls away, so it never covers a page's own sticky bar) and a small badge that stays in
# the bottom-left corner (the report button is bottom-right); a fixed badge also prints on every page.
BANNER = ('<div role="note" data-env="practice" style="margin:0;padding:6px 16px;background:#c2410c;color:#fff;'
          'font:600 14px/1.5 Prompt,Sarabun,system-ui,sans-serif;text-align:center">'
          'ระบบซ้อม · ข้อมูลไม่จริง · ห้ามใส่ข้อมูลผู้ป่วยจริง</div>'
          '<div aria-hidden="true" style="position:fixed;left:10px;bottom:10px;z-index:2147483000;pointer-events:none;'
          'padding:3px 10px;border-radius:99px;background:#c2410c;color:#fff;'
          'font:600 12px/1.5 Prompt,Sarabun,system-ui,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.25)">ระบบซ้อม</div>')
# Tables of a training round that go when it starts over (besides the hospital data and bookings):
# what people did, what they were shown, and HIS messages / code matches that point at the old records.
PRACTICE_TABLES = ("orchestra_activity", "patient_access_log", "his_inbox", "his_mappings", "notification_deliveries")


def name() -> str:
    return (os.environ.get("APP_ENVIRONMENT") or "production").strip().lower()


def is_practice() -> bool:
    return name() in PRACTICE_NAMES


def title(text: str) -> str:
    """A notification title as people should see it."""
    return PREFIX + text if is_practice() and not text.startswith(PREFIX) else text


def mark_page(html: bytes) -> bytes:
    """The strip after <body> and "[ซ้อม]" before the tab title (pages are small: done on each response)."""
    text = html.decode("utf-8", "replace")
    text = re.sub(r"(<title>)", r"\1" + PREFIX, text, count=1, flags=re.I)
    text, n = re.subn(r"(<body[^>]*>)", r"\1" + BANNER, text, count=1, flags=re.I)
    if not n:
        text = BANNER + text
    return text.encode("utf-8")


def manifest(raw: bytes) -> bytes:
    """The installed app's name on a practice system, so the two icons on a phone are told apart."""
    data = json.loads(raw)
    data["name"] = f"{data.get('name', 'ORchestraAI')} (ซ้อม)"
    data["short_name"] = f"{data.get('short_name', 'ORchestra')} ซ้อม"
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def reset_practice(store, admin, phrase: str, *, actor: dict | None = None) -> dict:
    """Practice system only: delete the training round's data and load fresh demo data. Accounts, their
    notification settings, problem reports and HIS keys stay; accounts linked to demo staff are linked again."""
    from . import hospital_io
    from .store import Forbidden, StoreError

    if not is_practice():
        raise Forbidden("ใช้ได้เฉพาะระบบซ้อม (APP_ENVIRONMENT=staging) เท่านั้น")
    if (phrase or "").strip() != RESET_PHRASE:
        raise StoreError(f'พิมพ์คำว่า "{RESET_PHRASE}" เพื่อยืนยัน')
    with store.db.connect() as conn:
        links = {r["user_id"]: r["employee_code"] for r in conn.execute(
            """SELECT u.user_id, s.employee_code FROM app_users u JOIN staff s ON s.staff_id = u.staff_id
               WHERE s.employee_code IS NOT NULL""")} if hospital_io._tables(conn) >= {"app_users", "staff"} else {}
    wiped = hospital_io.wipe_hospital(admin, hospital_io.WIPE_PHRASE, actor=actor)
    with store.db.connect() as conn:
        tables = hospital_io._tables(conn)
        for table in PRACTICE_TABLES:
            if table in tables:
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 (fixed names)
    seeded = store.seed_demo()
    relinked = 0
    with store.db.connect() as conn:
        for user_id, code in links.items():
            relinked += conn.execute(
                """UPDATE app_users SET staff_id = (SELECT staff_id FROM staff WHERE employee_code = %s LIMIT 1)
                   WHERE user_id = %s AND EXISTS (SELECT 1 FROM staff WHERE employee_code = %s)""",
                (code, user_id, code)).rowcount
    return {"deleted": wiped["deleted"], "booked": seeded["booked"], "history": seeded["history"],
            "relinked_users": relinked, "unlinked_users": len(links) - relinked}
