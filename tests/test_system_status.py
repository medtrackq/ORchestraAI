"""Admin "สถานะระบบ" page: which SQL files the database has (and running the missing ones),
the server settings without secret values, and the go-live checklist.

File-reading and settings tests need no database; the rest run against a real
PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

import json
import shutil
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app import system_status as ss
from app.db import Database
from test_db import ADMIN_URL, _rows, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
FILES = sorted(p.name for p in ss.MIGRATIONS.glob("*.sql"))


# ------------------------------------------------------------- files

def test_every_migration_file_is_read():
    items = ss.migrations()
    assert [m.name for m in items] == FILES
    assert not items[0].rerunnable and all(m.rerunnable for m in items[1:])  # only the first can't run twice
    blocks = next(m for m in items if m.name == "20261111000000_or_blocks.sql")
    assert blocks.tables == {"or_blocks", "or_block_releases"} and blocks.title == "บล็อกห้องผ่าตัดรายสัปดาห์"
    readiness = next(m for m in items if m.name == "20261108000000_case_readiness.sql")
    assert ("procedures", "requires_blood") in readiness.columns
    assert all(m.tables or m.columns for m in items)  # every file can be detected


def test_markers_ignore_comments():
    sql = """-- CREATE TABLE not_this (x int);
             CREATE TABLE IF NOT EXISTS public.Thing (x int); -- ALTER TABLE x ADD COLUMN nope int;
             ALTER TABLE staff ADD COLUMN IF NOT EXISTS a int, ADD COLUMN b text;
             ALTER TABLE rooms DROP COLUMN c;"""
    assert ss.markers(sql) == ({"thing"}, {("staff", "a"), ("staff", "b")})


def test_settings_never_show_secret_values(monkeypatch):
    secrets = {"APP_PASSWORD": "pw-secret-1", "LINE_CHANNEL_ACCESS_TOKEN": "tok-secret-2",
               "LINE_CHANNEL_SECRET": "line-secret-3", "ANTHROPIC_API_KEY": "sk-secret-4"}
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOSPITAL_NAME", "โรงพยาบาลทดสอบ")
    monkeypatch.setenv("READINESS_DIGEST_HOUR", "off")
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
    rows = {r["key"]: r for r in ss.settings("supabase", True, "ready")}
    text = json.dumps(rows, ensure_ascii=False)
    assert not any(value in text for value in secrets.values())
    assert rows["DATABASE_URL"]["ok"] and rows["ANTHROPIC_API_KEY"]["ok"]
    assert rows["LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET"]["ok"]
    assert rows["HOSPITAL_NAME"]["value"] == "โรงพยาบาลทดสอบ"
    assert (rows["READINESS_DIGEST_HOUR"]["ok"], rows["READINESS_DIGEST_HOUR"]["value"]) == (False, "ปิด")
    assert rows["APP_URL"]["ok"] is False
    demo = {r["key"]: r for r in ss.settings("demo", False, None)}
    assert demo["DATABASE_URL"]["ok"] is False and demo["DATABASE_URL"]["required"]


def test_checklist_names_missing_files_by_number():
    no_tables = SimpleNamespace(execute=lambda *args: [])
    rows = [{"status": "applied"}, {"status": "missing"}, {"status": "partial"}]
    sql, rls = ss.checklist(no_tables, rows, ["staff"], [])
    assert (sql["status"], sql["detail"], sql["link"]) == ("bad", "ยังขาด 2 ไฟล์: 002, 003", "#migrations")
    assert rls["status"] == "bad" and "staff" in rls["detail"]


def test_demo_mode_shows_settings_only():
    from app.store import ScheduleStore

    stub = SimpleNamespace(store=SimpleNamespace(ready=lambda: True, setting=lambda key: None))
    out = ss.summary(ScheduleStore(workers=2, time_limit_s=10), stub, SimpleNamespace(llm=SimpleNamespace(enabled=False)),
                     SimpleNamespace(store=SimpleNamespace(list=lambda: [])))
    assert out["mode"] == "demo" and out["settings"] and out["migrations"] == [] and out["checks"] == []


# ------------------------------------------------------------- database

@pytest.fixture()
def empty_url():
    name = "orchestra_empty_" + uuid.uuid4().hex[:10]
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    try:
        yield make_conninfo(ADMIN_URL, dbname=name)
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _status(url) -> dict:
    with Database(url).connect() as conn:
        return {m["name"]: m for m in ss.check(conn)}


@needs_db
def test_an_empty_database_gets_every_file(empty_url):
    before = _status(empty_url)
    assert {m["status"] for m in before.values()} == {"missing"} and all(m["runnable"] for m in before.values())
    results = ss.run_missing(Database(empty_url))
    assert [r["name"] for r in results] == FILES and all(r["ok"] for r in results)
    assert {m["status"] for m in _status(empty_url).values()} == {"applied"}
    with Database(empty_url).connect() as conn:
        assert ss.rls_off(conn) == []  # every table the files create has Row Level Security on
    assert ss.run_missing(Database(empty_url)) == []  # nothing left to do


@needs_db
def test_missing_later_files_are_run_and_data_kept(store, db_url):
    staff = _rows(db_url, "SELECT count(*) FROM staff")
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DROP TABLE case_milestones")
        conn.execute("DROP TABLE or_blocks CASCADE")
        conn.execute("ALTER TABLE procedures DROP COLUMN requires_blood")
    status = _status(db_url)
    assert status["20261108000000_case_readiness.sql"]["status"] == "partial"
    assert status["20261108000000_case_readiness.sql"]["missing"] == ["procedures.requires_blood"]
    assert status["20261110000000_case_milestones.sql"]["status"] == "missing"
    assert status["20261111000000_or_blocks.sql"]["status"] == "partial"  # or_block_releases is still there
    assert status["20261031000000_initial_schema.sql"]["status"] == "applied"
    results = ss.run_missing(store.db)
    assert [(r["name"], r["ok"]) for r in results] == [
        ("20261108000000_case_readiness.sql", True), ("20261110000000_case_milestones.sql", True),
        ("20261111000000_or_blocks.sql", True)]
    assert {m["status"] for m in _status(db_url).values()} == {"applied"}
    assert _rows(db_url, "SELECT count(*) FROM staff") == staff
    assert store.state()["milestones_ready"] is True  # the app uses the table at once, no restart
    ss.log_run(store, results, {"name": "Admin A", "role": "admin"})
    assert _rows(db_url, "SELECT title, actor FROM orchestra_activity ORDER BY created_at DESC LIMIT 1") == [
        ("รัน SQL 3 ไฟล์", "Admin A (admin)")]


@needs_db
def test_the_first_file_never_runs_on_a_database_with_tables(empty_url):
    with psycopg.connect(empty_url, autocommit=True) as conn:
        conn.execute("CREATE TABLE something_else (x int)")
    first = _status(empty_url)[FILES[0]]
    assert (first["status"], first["runnable"]) == ("missing", False)
    results = ss.run_missing(Database(empty_url))
    assert results == [{"name": FILES[0], "ok": False, "skipped": True,
                        "error": "ไฟล์นี้รันซ้ำไม่ได้ ต้องรันเองใน Supabase SQL Editor"}]
    assert _rows(empty_url, "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'") == [(1,)]


@needs_db
def test_a_failing_file_is_rolled_back_and_stops_the_run(db_url, tmp_path):
    shutil.copy(ss.MIGRATIONS / FILES[0], tmp_path / FILES[0])
    (tmp_path / "20990101000000_bad.sql").write_text(
        "-- Safe to run again.\nBEGIN;\nCREATE TABLE zz_one (x int);\nSELECT 1/0;\nCOMMIT;\n")
    (tmp_path / "20990102000000_next.sql").write_text("-- Safe to run again.\nCREATE TABLE zz_two (x int);\n")
    results = ss.run_missing(Database(db_url), items=ss.migrations(tmp_path))
    assert [(r["name"], r["ok"]) for r in results] == [("20990101000000_bad.sql", False)]
    assert "division by zero" in results[0]["error"]
    assert _rows(db_url, "SELECT to_regclass('zz_one'), to_regclass('zz_two')") == [(None, None)]
    (tmp_path / "20990101000000_bad.sql").unlink()
    assert [r["ok"] for r in ss.run_missing(Database(db_url), items=ss.migrations(tmp_path))] == [True]  # lock was freed
    assert _rows(db_url, "SELECT to_regclass('zz_two') IS NOT NULL") == [(True,)]


@needs_db
def test_go_live_checklist(store, db_url):
    from app.auth import User

    users = [User("u1", "dr.a", "Dr A", "surgeon"), User("u2", "nurse.b", "Nurse B", "staff", staff_id="x")]
    notifier = SimpleNamespace(store=SimpleNamespace(ready=lambda: True, setting=lambda key: None))
    out = ss.summary(store, notifier, SimpleNamespace(llm=SimpleNamespace(enabled=False)),
                     SimpleNamespace(store=SimpleNamespace(list=lambda: users)))
    assert out["mode"] == "supabase" and out["db_error"] is None and out["rls_off"] == []
    assert {m["status"] for m in out["migrations"]} == {"applied"}
    push = next(r for r in out["settings"] if r["key"] == "(สร้างเอง)")
    assert push["ok"] is True and "สร้างกุญแจเมื่อ" in push["value"]  # works with no setup
    checks = {c["label"]: c for c in out["checks"]}
    assert checks["SQL ครบทุกไฟล์"]["status"] == "ok"
    assert checks["ลบข้อมูลตัวอย่างแล้ว"]["status"] == "warn"  # seed_demo cases are still there
    assert checks["มีห้องผ่าตัด"]["status"] == "ok" and checks["มีบุคลากรครบทุกบทบาท"]["status"] == "ok"
    linked = checks["บัญชีแพทย์/พยาบาลผูกกับรายชื่อบุคลากร"]
    assert linked["status"] == "warn" and "dr.a" in linked["detail"] and "nurse.b" not in linked["detail"]
    assert all(c["link"] for c in out["checks"])

    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("ALTER TABLE case_milestones DISABLE ROW LEVEL SECURITY")
        conn.execute("UPDATE operating_rooms SET status = 'CLOSED'")
    out = ss.summary(store, notifier, SimpleNamespace(llm=SimpleNamespace(enabled=False)),
                     SimpleNamespace(store=SimpleNamespace(list=lambda: [])))
    checks = {c["label"]: c for c in out["checks"]}
    assert out["rls_off"] == ["case_milestones"] and checks["Row Level Security เปิดทุกตาราง"]["status"] == "bad"
    assert checks["SQL ครบทุกไฟล์"]["status"] == "ok"
    assert checks["มีห้องผ่าตัด"]["status"] == "bad"


@needs_db
def test_page_loads_on_a_database_without_tables(empty_url):
    from app.db_store import DatabaseStore

    s = DatabaseStore(empty_url, workers=2, time_limit_s=10)
    notifier = SimpleNamespace(store=SimpleNamespace(ready=lambda: False, setting=lambda key: None))
    out = ss.summary(s, notifier, SimpleNamespace(llm=SimpleNamespace(enabled=False)),
                     SimpleNamespace(store=SimpleNamespace(list=lambda: [])))
    assert out["db_error"] is None and out["migrations"][0]["runnable"]
    assert [c["label"] for c in out["checks"]] == ["SQL ครบทุกไฟล์", "Row Level Security เปิดทุกตาราง"]
    ss.log_run(s, [{"name": FILES[0], "ok": True}], {"name": "Admin", "role": "admin"})  # no activity table: no error
