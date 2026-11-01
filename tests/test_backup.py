"""Full backup (ZIP of every table) and restoring it; server starts, scheduled jobs and backups on the status page.

Most run against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app import backup, privacy, system_status
from app.auth import User
from app.store import StoreError
from test_app import OWNER, fresh, make_user, owner, session  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, MIGRATIONS, _request, _rows, _tomorrow, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
ADMIN = {"name": "Admin A", "username": "a", "role": "admin", "staff_id": None, "user_id": "u-admin"}


@pytest.fixture()
def target_url():
    """A second, empty database with every SQL file run (where a backup is restored)."""
    name = "orchestra_restore_" + uuid.uuid4().hex[:10]
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = make_conninfo(ADMIN_URL, dbname=name)
    try:
        with psycopg.connect(url, autocommit=True) as conn:
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                conn.execute(migration.read_text())
        yield url
    finally:
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _counts(url) -> dict[str, int]:
    with psycopg.connect(url) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r'")]
        return {t: conn.execute(f'SELECT count(*) FROM public."{t}"').fetchone()[0] for t in tables}  # noqa: S608


def _busy_day(store, db_url):
    """Bookings, a cancellation, an access-log row and activity: rows in tables with serial ids too."""
    day = _tomorrow(store)
    data = _request(store.state(day), "Dr.Napat", "Laparoscopic cholecystectomy", "HN-B1")
    assert store.slot_check(data, "09:00", commit=True, day=day)["booked"]
    case = next(b["case_id"] for b in store.state(day)["schedule"]["bookings"] if b["patient_hn"] == "HN-B1")
    store.cancel_booking(case, {"reason": "NPO", "note": "ดื่มนม"}, day=day, actor=ADMIN)
    privacy.AccessLog(store).record(User("u1", "nurse", "N", "staff"), "schedule", day, [case])


# ------------------------------------------------------------- backup and restore

@needs_db
def test_backup_restores_into_a_new_database(store, db_url, target_url, tmp_path):
    _busy_day(store, db_url)
    out, manifest = backup.dump(db_url)
    data = out.read()
    names = [t["name"] for t in manifest["tables"]]
    assert set(names) == set(_counts(db_url)) and len(names) >= 31
    with psycopg.connect(db_url) as conn:  # a table always comes after the tables it points to
        for child, parent in conn.execute("""SELECT conrelid::regclass::text, confrelid::regclass::text
                                             FROM pg_constraint WHERE contype = 'f'"""):
            assert names.index(parent.split(".")[-1]) < names.index(child.split(".")[-1]), (child, parent)
    assert {t["name"]: t["rows"] for t in manifest["tables"]} == _counts(db_url)
    assert "20261112000000_privacy.sql" in manifest["migrations"]
    assert backup.patients_in(manifest) == _counts(db_url)["surgery_requests"]
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert {"manifest.json", "restore.sql", "README.txt"} <= set(zf.namelist())
        assert "HN" in zf.read("README.txt").decode() and "\\copy public.\"staff\"" in zf.read("restore.sql").decode()

    path = tmp_path / "backup.zip"
    path.write_bytes(data)
    done = backup.restore(target_url, str(path))
    assert done["rows"] == sum(_counts(db_url).values()) and _counts(target_url) == _counts(db_url)
    assert _rows(target_url, "SELECT patient_hn FROM surgery_requests WHERE patient_hn = 'HN-B1'") == [("HN-B1",)]
    with psycopg.connect(target_url, autocommit=True) as conn:  # serial ids carry on after the restored rows
        new = conn.execute("""INSERT INTO patient_access_log (user_id, username, role, action) VALUES ('u', 'u', 'staff', 'schedule')
                              RETURNING access_id""").fetchone()[0]
        assert new > conn.execute("SELECT max(access_id) FROM patient_access_log WHERE access_id <> %s", (new,)).fetchone()[0]
    with pytest.raises(StoreError, match="มีข้อมูลอยู่แล้ว"):  # never on top of real data
        backup.restore(target_url, str(path))


@needs_db
def test_restore_sql_works_with_psql(store, db_url, target_url, tmp_path):
    if not shutil.which("psql"):
        pytest.skip("psql is not installed")
    _busy_day(store, db_url)
    out, _ = backup.dump(db_url)
    with zipfile.ZipFile(out) as zf:
        zf.extractall(tmp_path)
    run = subprocess.run(["psql", target_url, "-q", "-f", "restore.sql"], cwd=tmp_path, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert _counts(target_url) == _counts(db_url)


@needs_db
def test_restore_refuses_wrong_files_and_old_schemas(db_url, target_url, tmp_path):
    bad = tmp_path / "x.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(StoreError, match="ไม่ใช่ไฟล์สำรอง"):
        backup.restore(target_url, str(bad))
    out, _ = backup.dump(db_url)
    good = tmp_path / "b.zip"
    good.write_bytes(out.read())
    with psycopg.connect(target_url, autocommit=True) as conn:
        conn.execute("DROP TABLE or_block_releases")
    with pytest.raises(StoreError, match="ยังไม่มีตาราง or_block_releases"):
        backup.restore(target_url, str(good))


@needs_db
def test_command_line(store, db_url, target_url, tmp_path, monkeypatch, capsys):  # store: rows to restore
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert backup.main(["dump", str(tmp_path / "b.zip")]) == 2  # the URL comes from the environment only
    monkeypatch.setenv("DATABASE_URL", db_url)
    assert backup.main(["dump", str(tmp_path / "b.zip")]) == 0
    monkeypatch.setenv("DATABASE_URL", target_url)
    assert backup.main(["restore", str(tmp_path / "b.zip")]) == 0
    assert "กู้คืนแล้ว" in capsys.readouterr().out
    assert backup.main(["restore", str(tmp_path / "b.zip")]) == 1  # not empty any more


# ------------------------------------------------------------- status page

@needs_db
def test_status_page_tracks_starts_jobs_and_backups(store, db_url, monkeypatch):
    monkeypatch.setenv("READINESS_DIGEST_HOUR", "14")
    tz = store.db.tz
    afternoon = datetime.now(tz).replace(hour=16, minute=0)
    with store._transaction(write=False) as conn:
        info, checks = system_status.operations(conn, tz, afternoon)
    labels = {c["label"]: c for c in checks}
    assert labels["สำรองข้อมูล"]["status"] == "warn" and "ไม่มีการสำรองข้อมูลอัตโนมัติ" in labels["สำรองข้อมูล"]["detail"]
    assert labels["งานสรุปความพร้อมวันถัดไป"]["status"] == "warn"  # the 14:00 check has not run today

    for _ in range(4):
        system_status.record_boot(store)
    system_status.mark_job(store, "digest", afternoon.replace(hour=14, minute=5))
    _, manifest = backup.dump(db_url)
    system_status.note_backup(store, manifest, 12345, ADMIN)
    with store._transaction(write=False) as conn:
        info, checks = system_status.operations(conn, tz, afternoon)
    labels = {c["label"]: c for c in checks}
    assert info["boots_24h"] == 4 and "เริ่มใหม่ 4 ครั้ง" in labels["เซิร์ฟเวอร์ทำงานต่อเนื่อง"]["detail"]
    assert "งานสรุปความพร้อมวันถัดไป" not in labels and info["digest_last"].startswith(afternoon.date().isoformat())
    assert labels["สำรองข้อมูล"]["status"] == "ok" and info["backup"]["by"] == "Admin A (admin)"
    assert info["backup"]["rows"] == sum(t["rows"] for t in manifest["tables"])
    with store._transaction(write=False) as conn:  # a week later the backup is old
        _, checks = system_status.operations(conn, tz, afternoon + timedelta(days=8))
    assert {c["label"]: c for c in checks}["สำรองข้อมูล"]["status"] == "warn"

    privacy.set_retention(store, 365, None)  # retention on but never ran
    with store._transaction(write=False) as conn:
        _, checks = system_status.operations(conn, tz, afternoon)
    assert any(c["label"].startswith("งานลบข้อมูลผู้ป่วย") for c in checks)
    privacy.apply(store)
    with store._transaction(write=False) as conn:
        _, checks = system_status.operations(conn, tz, datetime.now(timezone.utc))
    assert not any(c["label"].startswith("งานลบข้อมูลผู้ป่วย") for c in checks)


@needs_db
def test_the_digest_timer_checks_in(store, db_url):
    from app.readiness import DigestTimer

    timer = DigestTimer(store, lambda event: None, hour=14)
    timer.tick(datetime.now(store.db.tz).replace(hour=9))
    assert _rows(db_url, "SELECT count(*) FROM app_settings WHERE key = 'job:digest'") == [(0,)]  # before 14:00
    timer.tick(datetime.now(store.db.tz).replace(hour=15))
    assert _rows(db_url, "SELECT count(*) FROM app_settings WHERE key = 'job:digest'") == [(1,)]


def test_backup_logged_as_patient_access_and_admin_only(owner):
    from app.main import access

    access._memory.clear()
    assert access.record(User("u1", "admin1", "A", "admin"), "backup", None, [], patients=42) is True
    entry = owner.get("/api/privacy/access").json()["entries"][0]
    assert (entry["action"], entry["patients"], entry["case_codes"]) == ("backup", 42, [])
    assert owner.get("/api/system/backup.zip").status_code == 400  # demo mode: nothing to back up
    scheduler = make_user(owner, "sched5", "scheduler")
    assert scheduler.get("/api/system/backup.zip").status_code == 403
    assert json.loads(owner.get("/api/system/status").text)["server"] is None
