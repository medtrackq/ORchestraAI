"""Admin page "สถานะระบบ": the database's SQL migrations, the server settings and a go-live checklist.

Migrations: every file in ``supabase/migrations`` is read and the tables / columns it
creates are looked up in the database, so each file shows as applied, partly applied
or not run. Files that say "Safe to run again" (all but the first) can be run from
the page, in order, each in its own transaction; the first (initial schema) only on
an empty database. Settings show whether each environment variable is set, never a
secret's value.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg import pq
from psycopg.rows import dict_row

from .auth import AuthError
from .hospital_io import DEMO_NAME, _demo_where
from .store import StoreError

log = logging.getLogger("orchestra.status")

MIGRATIONS = Path(__file__).resolve().parent.parent / "supabase" / "migrations"
SAFE = re.compile(r"safe to run again", re.IGNORECASE)
MIGRATE_LOCK = 7_734_102  # one person runs migrations at a time
TITLES = {  # Thai names for the page (files not listed show the first comment line)
    "20261031000000_initial_schema.sql": "โครงสร้างฐานข้อมูลเริ่มต้น (ห้อง บุคลากร หัตถการ คำขอ ตารางผ่าตัด)",
    "20261101000000_security_and_integrity.sql": "ความปลอดภัย (RLS) กันจองซ้อน และบันทึกกิจกรรม",
    "20261102000000_user_accounts.sql": "บัญชีผู้ใช้และบทบาท",
    "20261103000000_staff_fatigue_and_skills.sql": "ชั่วโมงทำงาน/เวลาพักของบุคลากร และทักษะที่หัตถการต้องการ",
    "20261104000000_hospital_data_and_room_closures.sql": "หน้าข้อมูลโรงพยาบาล ปิดห้องล่วงหน้า และเคสแพร่เชื้อทางอากาศ",
    "20261105000000_supply_inventory.sql": "วัสดุสิ้นเปลืองและรากเทียม (คลัง)",
    "20261106000000_facility_and_cssd.sql": "เวลาผ่าตัดนัดของห้อง เตียงพักฟื้น และแผนซ่อมบำรุงเครื่องมือ",
    "20261107000000_notifications.sql": "แจ้งเตือนบนมือถือ (เบราว์เซอร์และ LINE)",
    "20261108000000_case_readiness.sql": "เช็กลิสต์ความพร้อมก่อนผ่าตัด",
    "20261109000000_case_cancellations.sql": "เหตุผลการงด/เลื่อนผ่าตัด",
    "20261110000000_case_milestones.sql": "เวลาแต่ละขั้นของเคส (กระดานสถานะ)",
    "20261111000000_or_blocks.sql": "บล็อกห้องผ่าตัดรายสัปดาห์",
    "20261112000000_privacy.sql": "บันทึกการเข้าถึงข้อมูลผู้ป่วย และลบข้อมูลตามระยะเวลาเก็บ (PDPA)",
}
ROLES_TH = {"SURGEON": "ศัลยแพทย์", "ANESTHESIOLOGIST": "วิสัญญีแพทย์", "SCRUB_NURSE": "พยาบาลส่งเครื่องมือ",
            "CIRCULATING_NURSE": "พยาบาลช่วยรอบนอก"}


@dataclass
class Migration:
    name: str
    path: Path
    title: str
    tables: set[str] = field(default_factory=set)
    columns: set[tuple[str, str]] = field(default_factory=set)
    rerunnable: bool = False


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def markers(sql: str) -> tuple[set[str], set[tuple[str, str]]]:
    """Tables a script creates and columns it adds (how to tell whether it has run)."""
    body = _strip_comments(sql)
    tables = {t.lower() for t in re.findall(r"create\s+table\s+(?:if\s+not\s+exists\s+)?(?:public\.)?(\w+)", body, re.I)}
    columns = set()
    for table, rest in re.findall(r"alter\s+table\s+(?:if\s+exists\s+)?(?:only\s+)?(?:public\.)?(\w+)(.*?);", body,
                                  re.I | re.S):
        for col in re.findall(r"add\s+column\s+(?:if\s+not\s+exists\s+)?(\w+)", rest, re.I):
            columns.add((table.lower(), col.lower()))
    return tables, columns


def migrations(folder: Path = MIGRATIONS) -> list[Migration]:
    out = []
    for path in sorted(folder.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        first = next((line.strip("- ").strip() for line in sql.splitlines() if line.startswith("--")), "")
        title = TITLES.get(path.name) or re.sub(r"^ORchestraAI migration \d+:\s*", "", first)
        tables, columns = markers(sql)
        out.append(Migration(path.name, path, title, tables, columns, bool(SAFE.search(sql))))
    return out


def _schema(conn) -> tuple[set[str], set[tuple[str, str]]]:
    tables = {r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    cols = {(r["table_name"], r["column_name"]) for r in conn.execute(
        "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public'")}
    return tables, cols


def check(conn, items: list[Migration] | None = None) -> list[dict]:
    """Each migration file: applied / partial / missing / unknown, and whether the page can run it."""
    tables, cols = _schema(conn)
    out = []
    items = items if items is not None else migrations()
    for i, m in enumerate(items):
        need = len(m.tables) + len(m.columns)
        have = len(m.tables & tables) + len(m.columns & cols)
        status = "unknown" if not need else "applied" if have == need else "missing" if have == 0 else "partial"
        initial = i == 0
        runnable = status in ("missing", "partial") and (m.rerunnable or (initial and status == "missing" and not tables))
        out.append({"name": m.name, "title": m.title, "status": status, "have": have, "need": need,
                    "missing": sorted([t for t in m.tables if t not in tables]
                                      + [f"{t}.{c}" for t, c in m.columns if (t, c) not in cols])[:6],
                    "runnable": runnable, "rerunnable": m.rerunnable})
    return out


def rls_off(conn) -> list[str]:
    """Public tables without Row Level Security (readable through Supabase's API with the anon key)."""
    return [r["relname"] for r in conn.execute(
        """SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'
           AND NOT relrowsecurity ORDER BY relname""")]


def run_missing(db, *, items: list[Migration] | None = None) -> list[dict]:
    """Run, in order, every migration the database lacks and the page may run; stop at the first error.

    Uses a connection of its own in autocommit mode so each file's BEGIN ... COMMIT
    is its own transaction; an advisory lock keeps two admins from running at once.
    """
    from .db import check_url

    items = items if items is not None else migrations()
    problem = check_url(db.url)
    if problem:
        raise StoreError(f"เชื่อมต่อฐานข้อมูลไม่ได้: {problem}")
    kwargs = {"autocommit": True, "connect_timeout": 15, "prepare_threshold": None, "row_factory": dict_row}
    local = any(h in db.url for h in ("localhost", "127.0.0.1", "/var/run", "host=/"))
    if "sslmode=" not in db.url and not local:
        kwargs["sslmode"] = "require"
    try:
        conn = psycopg.connect(db.url, **kwargs)
    except psycopg.Error as exc:
        raise StoreError(f"เชื่อมต่อฐานข้อมูลไม่ได้: {str(exc).splitlines()[0]}") from exc
    results: list[dict] = []
    with conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (MIGRATE_LOCK,)).fetchone()["ok"]:
            raise StoreError("มีคนกำลังรัน SQL อยู่ กรุณารอสักครู่แล้วลองใหม่")
        try:
            plan = check(conn, items)  # looked up under the lock: another run may just have finished
            for m, p in zip(items, plan):
                if p["status"] not in ("missing", "partial"):
                    continue
                if not p["runnable"]:
                    results.append({"name": m.name, "ok": False, "skipped": True,
                                    "error": "ไฟล์นี้รันซ้ำไม่ได้ ต้องรันเองใน Supabase SQL Editor"})
                    break
                sql = m.path.read_text(encoding="utf-8")
                if not re.search(r"^\s*BEGIN\s*;", sql, re.I | re.M):
                    sql = f"BEGIN;\n{sql}\nCOMMIT;"
                try:
                    conn.execute(sql)
                    results.append({"name": m.name, "ok": True})
                except psycopg.Error as exc:
                    if conn.info.transaction_status != pq.TransactionStatus.IDLE:
                        conn.execute("ROLLBACK")
                    message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                    log.error("migration %s failed: %s", m.name, message)
                    results.append({"name": m.name, "ok": False, "error": message})
                    break
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATE_LOCK,))
    return results


# ------------------------------------------------------------------ settings

def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def settings(mode: str, llm_enabled: bool, push_state: str | None) -> list[dict]:
    """What the server is set up with (no secret values)."""
    digest = _env("READINESS_DIGEST_HOUR") or "14"
    url = _env("APP_URL") or _env("RENDER_EXTERNAL_URL")
    line = bool(_env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_CHANNEL_SECRET"))
    rows = [
        ("DATABASE_URL", "ฐานข้อมูล Supabase", mode == "supabase",
         "เชื่อมแล้ว" if mode == "supabase" else "ไม่ได้ตั้ง: ใช้ข้อมูลจำลองในหน่วยความจำ (หายเมื่อรีสตาร์ต)", True),
        ("APP_USERNAME / APP_PASSWORD", "บัญชีหลักของเจ้าของระบบ", len(_env("APP_PASSWORD")) >= 12,
         (f"ชื่อผู้ใช้ {_env('APP_USERNAME') or 'orchestra'}"
          + (" · รหัสผ่านสั้นกว่า 12 ตัวอักษร ควรเปลี่ยนให้ยาวขึ้น" if len(_env("APP_PASSWORD")) < 12 else ""))
         if _env("APP_PASSWORD") else "ไม่ได้ตั้งรหัสผ่าน", True),
        ("SESSION_SECRET", "กุญแจลงชื่อ session ของการเข้าสู่ระบบ", bool(_env("SESSION_SECRET")),
         "ตั้งแล้ว" if _env("SESSION_SECRET") else "ไม่ได้ตั้ง: ใช้กุญแจที่คำนวณจาก APP_PASSWORD (ควรตั้งเป็นค่าสุ่มยาว ๆ แยกต่างหาก)",
         False),
        ("ANTHROPIC_API_KEY", "Intake Agent อ่านคำขอด้วย Claude", llm_enabled,
         "ใช้ Claude" if llm_enabled else "ไม่ได้ตั้ง: ใช้กฎคำสำคัญแทน (ยังใช้ได้)", False),
        ("LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET", "แจ้งเตือนทาง LINE", line,
         "ตั้งแล้ว" + (" · มีลิงก์เพิ่มเพื่อน" if _env("LINE_ADD_FRIEND_URL") else " · ยังไม่มี LINE_ADD_FRIEND_URL")
         if line else "ไม่ได้ตั้ง: ใช้แจ้งเตือนในเบราว์เซอร์ได้อย่างเดียว", False),
        ("(สร้างเอง)", "แจ้งเตือนในเบราว์เซอร์ (Web Push)", push_state in ("ready", "no_key"),
         {"ready": "พร้อม (สร้างกุญแจแล้ว)", "no_key": "พร้อมใช้ · สร้างกุญแจเมื่อมีคนเปิดแจ้งเตือนครั้งแรก",
          None: "ใช้ไม่ได้: ยังไม่ได้รัน SQL การแจ้งเตือน"}.get(push_state, push_state), False),
        ("APP_URL", "ที่อยู่เว็บสำหรับลิงก์ในข้อความ LINE", bool(url), url or "ไม่ได้ตั้ง: ข้อความ LINE จะไม่มีลิงก์", False),
        ("HOSPITAL_NAME", "ชื่อโรงพยาบาลบนกระดาษพิมพ์และกระดานสถานะ", bool(_env("HOSPITAL_NAME")),
         _env("HOSPITAL_NAME") or "ไม่ได้ตั้ง (ไม่บังคับ)", False),
        ("READINESS_DIGEST_HOUR", "เวลาส่งสรุปเคสวันถัดไปที่ยังไม่พร้อม", digest.lower() != "off",
         "ปิด" if digest.lower() in ("off", "none", "-1") else f"{digest}:00 น." + ("" if _env("READINESS_DIGEST_HOUR")
                                                                                 else " (ค่าตั้งต้น)"), False),
        ("APP_TIMEZONE", "เขตเวลา", True, _env("APP_TIMEZONE") or "Asia/Bangkok (ค่าตั้งต้น)", False),
        ("SOLVER_WORKERS / SOLVER_TIME_LIMIT", "กำลังคำนวณตาราง", True,
         f"{_env('SOLVER_WORKERS') or '2'} worker · ไม่เกิน {_env('SOLVER_TIME_LIMIT') or '10'} วินาที", False),
    ]
    return [{"key": k, "label": label, "ok": ok, "value": value, "required": required}
            for k, label, ok, value, required in rows]


def version() -> str | None:
    commit = _env("RENDER_GIT_COMMIT")
    return commit[:7] if commit else None


# ------------------------------------------------------------------ go-live checklist

def checklist(conn, migration_rows: list[dict], rls: list[str], users: list) -> list[dict]:
    """Things to have in place before real patients are booked (status: ok / warn / bad / info)."""
    out = []

    def add(label, status, detail, link=None):
        out.append({"label": label, "status": status, "detail": detail, "link": link})

    missing = [f"{i:03d}" for i, m in enumerate(migration_rows, 1) if m["status"] in ("missing", "partial")]
    add("SQL ครบทุกไฟล์", "ok" if not missing else "bad",
        "รันครบแล้ว" if not missing else f"ยังขาด {len(missing)} ไฟล์: " + ", ".join(missing), "#migrations")
    add("Row Level Security เปิดทุกตาราง", "ok" if not rls else "bad",
        "เปิดครบ" if not rls else "ยังไม่เปิด: " + ", ".join(rls[:6]) + " (ข้อมูลอาจถูกอ่านผ่าน Supabase API ได้)", "#migrations")
    tables = {r["table_name"] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    if "staff" not in tables:
        return out
    where, args = _demo_where()
    demo = conn.execute(
        f"""SELECT count(*) FILTER (WHERE r.patient_hn LIKE 'HIST-%%') AS hist,
                   count(*) FILTER (WHERE r.patient_name LIKE %s) AS demo
            FROM surgery_requests r WHERE {where}""", (DEMO_NAME, *args)).fetchone()
    add("ลบข้อมูลตัวอย่างแล้ว", "ok" if not (demo["hist"] or demo["demo"]) else "warn",
        "ไม่มีข้อมูลตัวอย่าง" if not (demo["hist"] or demo["demo"]) else
        f"ยังมีเคสตัวอย่าง {demo['demo']} และประวัติตัวอย่าง {demo['hist']} เคส (รายงานและการพยากรณ์ยังใช้อยู่)", "/admin#io")
    rooms = conn.execute("SELECT count(*) AS n FROM operating_rooms WHERE COALESCE(status, 'AVAILABLE') = 'AVAILABLE'").fetchone()["n"]
    add("มีห้องผ่าตัด", "ok" if rooms else "bad", f"{rooms} ห้องที่เปิดใช้", "/admin#rooms")
    roles = {r["role"]: r["n"] for r in conn.execute("SELECT role, count(*) AS n FROM staff WHERE is_active GROUP BY role")}
    lacking = [ROLES_TH[r] for r in ROLES_TH if not roles.get(r)]
    add("มีบุคลากรครบทุกบทบาท", "ok" if not lacking else "bad",
        " · ".join(f"{ROLES_TH[r]} {roles.get(r, 0)}" for r in ROLES_TH) + (f" · ยังไม่มี: {', '.join(lacking)}" if lacking else ""),
        "/admin#staff")
    week = conn.execute(
        """SELECT p.role, count(DISTINCT p.staff_id) AS n FROM staff p JOIN staff_schedules s USING (staff_id)
           WHERE p.is_active AND s.shift_type IS DISTINCT FROM 'LEAVE' AND s.shift_end > now()
             AND s.shift_start < now() + interval '7 days' GROUP BY p.role""").fetchall()
    on = {r["role"]: r["n"] for r in week}
    no_shift = [ROLES_TH[r] for r in ROLES_TH if roles.get(r) and not on.get(r)]
    add("มีเวรใน 7 วันข้างหน้า", "ok" if not no_shift and on else "warn",
        " · ".join(f"{ROLES_TH[r]} {on.get(r, 0)}/{roles.get(r, 0)} คน" for r in ROLES_TH if roles.get(r))
        + (f" · ไม่มีเวรเลย: {', '.join(no_shift)} (ระบบจัดคนเข้าเคสไม่ได้)" if no_shift else ""), "/admin#roster")
    no_spec = conn.execute("""SELECT count(*) AS n FROM staff WHERE is_active AND role = 'SURGEON'
                              AND COALESCE(trim(specialty), '') = ''""").fetchone()["n"]
    add("ศัลยแพทย์มีความเชี่ยวชาญ (ใช้กับบล็อกห้องแบบแผนก)", "ok" if not no_spec else "info",
        "ครบทุกคน" if not no_spec else f"ยังไม่ได้ใส่ {no_spec} คน", "/admin#staff")
    unlinked = [u.username for u in users if u.role in ("surgeon", "staff") and not u.staff_id and u.is_active]
    add("บัญชีแพทย์/พยาบาลผูกกับรายชื่อบุคลากร", "ok" if not unlinked else "warn",
        "ผูกครบ" if not unlinked else f"ยังไม่ผูก {len(unlinked)} บัญชี: {', '.join(unlinked[:5])} (ไม่ได้รับแจ้งเตือนเคสของตัวเอง)",
        "/#users")
    accounts = [u for u in users if u.is_active and u.user_id != "env-admin"]
    add("มีบัญชีผู้ใช้ของแต่ละคน", "ok" if len(accounts) > 1 else "info",
        f"{len(accounts)} บัญชี" + ("" if len(accounts) > 1 else " · ควรสร้างบัญชีของแต่ละคน แทนการใช้บัญชีหลักร่วมกัน"), "/#users")
    logged = "patient_access_log" in tables
    add("บันทึกการเข้าถึงข้อมูลผู้ป่วย (PDPA)", "ok" if logged else "bad",
        "บันทึกทุกครั้งที่มีคนเห็น HN / ชื่อผู้ป่วย" if logged else "ยังไม่ได้รัน SQL ไฟล์ 013: ยังไม่มีบันทึกว่าใครเปิดดูข้อมูลผู้ป่วย",
        "/privacy" if logged else "#migrations")
    days = None
    if "app_settings" in tables:
        row = conn.execute("SELECT value FROM app_settings WHERE key = 'retention_patient_days'").fetchone()
        days = row["value"] if row else None
    add("กำหนดระยะเวลาเก็บข้อมูลผู้ป่วย (PDPA)", "ok" if days else "warn",
        f"ลบ HN ชื่อ และข้อความอิสระ {days} วันหลังผ่าตัด" if days else "ยังไม่ได้กำหนด: ข้อมูลระบุตัวผู้ป่วยถูกเก็บไว้ไม่มีกำหนด",
        "/privacy")
    return out


# ------------------------------------------------------------------ server, scheduled jobs, backups

STARTED = datetime.now(timezone.utc)  # this server process
BACKUP_WARN_DAYS = 7


def _set(store, key: str, value: str) -> None:
    """Write one app_settings value (only once migration 008 made the table); never raises."""
    try:
        with store.db.connect() as conn:
            if conn.execute("SELECT to_regclass('public.app_settings') IS NOT NULL AS ok").fetchone()["ok"]:
                conn.execute("""INSERT INTO app_settings (key, value) VALUES (%s, %s)
                                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (key, value))
    except Exception as exc:  # bookkeeping must never break the job it describes
        log.warning("could not save %s: %s", key, exc)


def record_boot(store) -> None:
    """Remember when this server started (the last 30 starts): frequent starts mean it sleeps when idle."""
    if getattr(store, "mode", "demo") != "supabase":
        return
    try:
        with store.db.connect() as conn:
            if not conn.execute("SELECT to_regclass('public.app_settings') IS NOT NULL AS ok").fetchone()["ok"]:
                return
            row = conn.execute("SELECT value FROM app_settings WHERE key = 'server_boots' FOR UPDATE").fetchone()
            boots = json.loads(row["value"]) if row else []
            boots = (boots + [STARTED.isoformat(timespec="seconds")])[-30:]
            conn.execute("""INSERT INTO app_settings (key, value) VALUES ('server_boots', %s)
                            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (json.dumps(boots),))
    except Exception as exc:
        log.warning("could not record the server start: %s", exc)


def mark_job(store, name: str, now: datetime | None = None) -> None:
    """A scheduled job checked in (``job:<name>``)."""
    if getattr(store, "mode", "demo") == "supabase":
        _set(store, f"job:{name}", (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))


def note_backup(store, manifest: dict, size: int, actor: dict | None) -> None:
    _set(store, "backup_last", json.dumps({
        "at": manifest["created_at"], "by": None if actor is None else f"{actor.get('name')} ({actor.get('role')})",
        "tables": len(manifest["tables"]), "rows": sum(t["rows"] for t in manifest["tables"]), "size": size,
    }, ensure_ascii=False))


def operations(conn, tz, now: datetime | None = None) -> tuple[dict, list[dict]]:
    """Server starts, scheduled jobs and the last backup, and what the go-live checklist says about them."""
    from .readiness import digest_hour

    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    setting = {}
    has_settings = conn.execute("SELECT to_regclass('public.app_settings') IS NOT NULL AS ok").fetchone()["ok"]
    if has_settings:
        setting = {r["key"]: r["value"] for r in conn.execute(
            """SELECT key, value FROM app_settings WHERE key IN ('server_boots', 'backup_last', 'job:digest',
                   'retention_last_run', 'retention_patient_days')""")}
    parse = lambda v: datetime.fromisoformat(v).astimezone(tz) if v else None  # noqa: E731
    boots = [parse(b) for b in json.loads(setting.get("server_boots") or "[]")]
    recent = [b for b in boots if b and now - b < timedelta(hours=24)]
    backup = json.loads(setting["backup_last"]) if setting.get("backup_last") else None
    hour = digest_hour()
    digest = parse(setting.get("job:digest"))
    retention = parse(setting.get("retention_last_run"))
    info = {"started_at": STARTED.astimezone(tz).isoformat(timespec="minutes"),
            "uptime_min": int((now - STARTED.astimezone(tz)).total_seconds() // 60),
            "boots_24h": len(recent), "backup": backup, "digest_hour": hour,
            "digest_last": digest.isoformat(timespec="minutes") if digest else None,
            "retention_last": retention.isoformat(timespec="minutes") if retention else None,
            "retention_on": bool(setting.get("retention_patient_days"))}
    checks = []

    def add(label, status, detail, link="#server"):
        checks.append({"label": label, "status": status, "detail": detail, "link": link})

    if backup is None:
        add("สำรองข้อมูล", "warn", "ยังไม่เคยดาวน์โหลดไฟล์สำรอง · Supabase แผนฟรีไม่มีการสำรองข้อมูลอัตโนมัติ")
    else:
        age = (now - parse(backup["at"])).days
        add("สำรองข้อมูล", "ok" if age < BACKUP_WARN_DAYS else "warn",
            f"ล่าสุด {age} วันก่อน" + ("" if age < BACKUP_WARN_DAYS else f" (ควรทำอย่างน้อยทุก {BACKUP_WARN_DAYS} วัน)"))
    if len(recent) >= 4:
        add("เซิร์ฟเวอร์ทำงานต่อเนื่อง", "warn",
            f"เริ่มใหม่ {len(recent)} ครั้งใน 24 ชม. · Render แผนฟรีหลับเมื่อไม่มีคนใช้ 15 นาที คนแรกต้องรอ และงานตั้งเวลาอาจไม่ทำงาน")
    # Jobs note their runs in app_settings (SQL file 008): without it there is nothing to check yet.
    if has_settings and hour is not None and now.hour >= hour + 1 and (digest is None or digest.date() != now.date()):
        add("งานสรุปความพร้อมวันถัดไป", "warn",
            f"วันนี้ยังไม่ได้ทำงาน (ตั้งไว้ {hour:02d}:00) · เซิร์ฟเวอร์อาจหลับอยู่ตอนนั้น จะทำงานเมื่อมีคนเปิดเว็บ")
    if info["retention_on"] and (retention is None or now - retention > timedelta(hours=26)):
        add("งานลบข้อมูลผู้ป่วยตามระยะเวลา (PDPA)", "warn",
            "ไม่ได้ทำงานเกิน 1 วัน (ตั้งไว้ 02:00) · เซิร์ฟเวอร์อาจหลับอยู่ตอนนั้น", "/privacy")
    return info, checks


def summary(store, notifier, intake, accounts) -> dict:
    """Everything the page shows."""
    mode = getattr(store, "mode", "demo")
    push_state = None
    try:
        if notifier.store.ready():
            push_state = "ready" if notifier.store.setting("vapid_private_pem") else "no_key"
    except Exception:  # the page must load even if notifications are broken
        push_state = None
    out = {"mode": mode, "version": version(), "settings": settings(mode, intake.llm.enabled, push_state),
           "migrations": [], "rls_off": [], "checks": [], "db_error": None, "server": None}
    if mode != "supabase":
        return out
    try:
        with store._transaction(write=False) as conn:
            out["migrations"] = check(conn)
            out["rls_off"] = rls_off(conn)
            out["checks"] = checklist(conn, out["migrations"], out["rls_off"], _users(accounts))
            out["server"], ops = operations(conn, store.db.tz)
            out["checks"] += ops
    except (StoreError, psycopg.Error) as exc:
        out["db_error"] = str(exc).splitlines()[0]
    return out


def _users(accounts) -> list:
    try:
        return accounts.store.list()
    except AuthError:  # no app_users table yet: the env admin only
        return []


def log_run(store, results: list[dict], actor: dict | None) -> None:
    """Note the run in the activity log (the table may only exist after this run)."""
    done = [r["name"] for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    if not results:
        return
    title = (f"รัน SQL {len(done)} ไฟล์" if done else "รัน SQL ไม่สำเร็จ") + (" (มีไฟล์ที่ไม่สำเร็จ)" if done and failed else "")
    lines = [f"สำเร็จ: {name}" for name in done] + [f"ไม่สำเร็จ: {r['name']}: {r.get('error')}" for r in failed]
    try:
        with store.db.connect() as conn:
            cols = {(r["table_name"], r["column_name"]) for r in conn.execute(
                """SELECT table_name, column_name FROM information_schema.columns
                   WHERE table_schema = 'public' AND table_name = 'orchestra_activity'""")}
            world = SimpleNamespace(day=store.db.today(), has_activity=("orchestra_activity", "activity_id") in cols,
                                    has_actor_col=("orchestra_activity", "actor") in cols)
            actor_text = None if actor is None else f"{actor.get('name')} ({actor.get('role')})"
            store.db.log_activity(conn, world, {"kind": "admin", "title": title, "log": lines, "changes": [],
                                                "notifications": [], "actor": actor_text})
    except Exception as exc:  # the SQL ran; a missing log line must not report it as failed
        log.warning("could not log the migration run: %s", exc)
