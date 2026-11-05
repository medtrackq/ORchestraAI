"""Nightly encrypted backup to Supabase Storage: encryption, the Storage calls (against a small fake of the
Storage REST API on 127.0.0.1), keeping and removing files, the nightly timer, the admin API and the
command line.

The database tests run against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

import io
import json
import threading
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import autobackup as ab, backup, privacy, system_status
from app.store import StoreError
from test_app import OWNER, fresh, make_user, owner, session  # noqa: F401  (fixtures)
from test_backup import _busy_day, _counts, target_url  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, _rows, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
KEY = "test-service-role-key"
PASSPHRASE = "correct horse battery staple 42"


# ------------------------------------------------------------------ a fake Supabase Storage

class FakeStorage:
    def __init__(self) -> None:
        self.buckets: dict[str, dict] = {}
        self.fail_upload: int | None = None
        self.uploads = 0
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def put(self, name: str, data: bytes = b"x", bucket: str = "orchestra-backups") -> None:
        self.buckets.setdefault(bucket, {"public": False, "objects": {}})["objects"][name] = data

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, content_type="application/json"):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length") or 0))

            def _authorised(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {KEY}" or self.headers.get("apikey") != KEY:
                    self._send(401, {"error": "Invalid JWT"})
                    return False
                return True

            def _parts(self):
                return [unquote(p) for p in self.path.split("?")[0].split("/")[3:]]  # after /storage/v1

            def do_GET(self):  # noqa: N802
                if not self._authorised():
                    return
                parts = self._parts()
                if parts[0] == "bucket":
                    found = fake.buckets.get(parts[1])
                    return self._send(200, {"id": parts[1], "name": parts[1], "public": found["public"]}) if found \
                        else self._send(400, {"statusCode": "404", "error": "Bucket not found"})
                data = fake.buckets.get(parts[1], {}).get("objects", {}).get(parts[2])
                return self._send(200, data, "application/octet-stream") if data is not None else self._send(400, {"error": "not_found"})

            def do_POST(self):  # noqa: N802
                if not self._authorised():
                    return
                parts, body = self._parts(), self._body()
                if parts == ["bucket"]:
                    spec = json.loads(body)
                    if spec["id"] in fake.buckets:
                        return self._send(409, {"error": "Duplicate", "message": "The resource already exists"})
                    fake.buckets[spec["id"]] = {"public": spec["public"], "objects": {}}
                    return self._send(200, {"name": spec["id"]})
                if parts[:2] == ["object", "list"]:
                    objects = fake.buckets.get(parts[2], {}).get("objects", {})
                    return self._send(200, [{"name": n, "created_at": "2026-11-02T00:00:00Z", "metadata": {"size": len(d)}}
                                            for n, d in sorted(objects.items(), reverse=True)])
                fake.uploads += 1
                if fake.fail_upload:
                    return self._send(fake.fail_upload, {"error": "Payload too large"})
                fake.buckets[parts[1]]["objects"][parts[2]] = body
                return self._send(200, {"Key": f"{parts[1]}/{parts[2]}"})

            def do_DELETE(self):  # noqa: N802
                if not self._authorised():
                    return
                parts, names = self._parts(), json.loads(self._body())["prefixes"]
                for n in names:
                    fake.buckets[parts[1]]["objects"].pop(n, None)
                return self._send(200, [{"name": n} for n in names])

        return Handler


@pytest.fixture()
def fake_storage(monkeypatch):
    fake = FakeStorage()
    monkeypatch.setenv("SUPABASE_URL", fake.url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", PASSPHRASE)
    monkeypatch.delenv("BACKUP_HOUR", raising=False)
    monkeypatch.delenv("BACKUP_KEEP_DAYS", raising=False)
    yield fake
    fake.server.shutdown()


def _name(day, hhmm="0300") -> str:
    return f"orchestra-{day.isoformat()}-{hhmm}.orcbak"


# ------------------------------------------------------------------ encryption

def test_files_open_only_whole_unchanged_and_with_the_passphrase(monkeypatch):
    monkeypatch.setattr(ab, "CHUNK", 1000)
    data = bytes(range(256)) * 20  # 5 chunks
    sealed = io.BytesIO()
    ab.encrypt(io.BytesIO(data), sealed, PASSPHRASE)
    raw = sealed.getvalue()
    assert raw.startswith(ab.MAGIC) and data[:200] not in raw
    out = io.BytesIO()
    assert ab.decrypt(io.BytesIO(raw), out, PASSPHRASE) == len(data) and out.getvalue() == data

    head = len(ab.MAGIC) + ab.SALT_LEN + ab.PREFIX_LEN
    chunk = 4 + 1000 + 16
    flipped = bytearray(raw)
    flipped[head + 50] ^= 1
    swapped = raw[:head] + raw[head + chunk:head + 2 * chunk] + raw[head:head + chunk] + raw[head + 2 * chunk:]
    for label, bad, phrase in (("wrong passphrase", raw, "another passphrase 1234"), ("changed byte", bytes(flipped), PASSPHRASE),
                               ("chunks reordered", swapped, PASSPHRASE), ("cut at a chunk", raw[:head + 2 * chunk], PASSPHRASE),
                               ("cut inside", raw[:-7], PASSPHRASE), ("not ours", b"PK\x03\x04zip", PASSPHRASE)):
        with pytest.raises(StoreError):
            ab.decrypt(io.BytesIO(bad), io.BytesIO(), phrase)
        assert label
    empty = io.BytesIO()
    ab.encrypt(io.BytesIO(b""), empty, PASSPHRASE)
    empty.seek(0)
    assert ab.decrypt(empty, io.BytesIO(), PASSPHRASE) == 0


def test_settings_project_address_and_what_is_kept(monkeypatch):
    for var in ("SUPABASE_URL", "BACKUP_ENCRYPTION_KEY", "SUPABASE_SERVICE_ROLE_KEY", "BACKUP_BUCKET"):
        monkeypatch.delenv(var, raising=False)
    ref = "abcdefghijklmnopqrst"
    assert ab.project_url(f"postgresql://postgres:pw@db.{ref}.supabase.co:5432/postgres") == f"https://{ref}.supabase.co"
    assert ab.project_url(f"postgresql://postgres.{ref}:pw@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres") \
        == f"https://{ref}.supabase.co"
    assert ab.project_url("postgresql://postgres@127.0.0.1:5432/x") is None
    config = ab.Config.from_env(f"postgresql://postgres:pw@db.{ref}.supabase.co:5432/postgres")
    assert not config.ready and len(config.missing()) == 2 and config.hour == 3 and config.keep_days == 14
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "short")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    assert ab.Config.from_env(None).missing() == ["BACKUP_ENCRYPTION_KEY ต้องยาวอย่างน้อย 16 ตัวอักษร",
                                                  "SUPABASE_URL (หาที่อยู่โปรเจกต์จาก DATABASE_URL ไม่ได้)"]
    assert "short" not in json.dumps(ab.Config.from_env(None).public()) and KEY not in json.dumps(ab.Config.from_env(None).public())

    today = datetime(2026, 11, 20).date()
    files = [{"name": _name(today - timedelta(days=d))} for d in (0, 1, 15, 16, 30)] + [{"name": "notes.txt"}]
    assert ab.to_remove(files, today, 14) == [_name(today - timedelta(days=16)), _name(today - timedelta(days=30))]
    only_old = [{"name": _name(today - timedelta(days=d))} for d in (40, 41, 42, 43)]
    assert ab.to_remove(only_old, today, 14) == [_name(today - timedelta(days=43))]  # the newest 3 always stay


# ------------------------------------------------------------------ against the database

@needs_db
def test_nightly_backup_is_encrypted_kept_pruned_and_restores(store, db_url, target_url, fake_storage, monkeypatch, tmp_path):
    _busy_day(store, db_url)
    today = store.db.today()
    for days in (1, 2, 20, 21):
        fake_storage.put(_name(today - timedelta(days=days)))
    fake_storage.put("someone-elses-file.pdf")
    access = privacy.AccessLog(store)
    result = ab.run(store, access)
    assert result["ok"] and result["removed"] == 2 and result["by"] == "ระบบ (ทุกคืน)"
    objects = fake_storage.buckets["orchestra-backups"]["objects"]
    assert result["name"] in objects and _name(today - timedelta(days=1)) in objects
    assert _name(today - timedelta(days=20)) not in objects and _name(today - timedelta(days=21)) not in objects  # > 14 days
    assert "someone-elses-file.pdf" in objects  # only our own files are ever removed
    sealed = objects[result["name"]]
    assert sealed.startswith(ab.MAGIC) and b"HN-B1" not in sealed and len(sealed) == result["size"]

    plain = io.BytesIO()
    ab.decrypt(io.BytesIO(sealed), plain, PASSPHRASE)
    assert "manifest.json" in zipfile.ZipFile(plain).namelist()
    assert _rows(db_url, "SELECT role, action, patients > 0 FROM patient_access_log WHERE action = 'backup'") == [("system", "backup", True)]
    with store.db.connect() as conn:
        assert ab.last(conn)["ok"]
        info, checks = system_status.operations(conn, store.db.tz)
    assert info["auto_backup"]["config"]["configured"] and info["backup"]["size"] == result["size"]
    assert next(c for c in checks if c["label"] == "สำรองข้อมูล")["detail"].startswith("อัตโนมัติทุกคืน")

    path = tmp_path / result["name"]
    path.write_bytes(sealed)
    assert backup.main(["decrypt", str(path), str(tmp_path / "plain.zip")]) == 0
    assert zipfile.is_zipfile(tmp_path / "plain.zip")
    monkeypatch.setenv("DATABASE_URL", target_url)
    assert backup.main(["restore", str(path)]) == 0  # straight from the encrypted file
    later = {"patient_access_log", "app_settings"}  # written after the backup was taken (its own log and result)
    assert {t: n for t, n in _counts(target_url).items() if t not in later} == \
        {t: n for t, n in _counts(db_url).items() if t not in later}
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "a wrong passphrase 000")
    assert backup.main(["decrypt", str(path), str(tmp_path / "x.zip")]) == 1


@needs_db
def test_failed_nights_are_recorded_tried_three_times_then_tomorrow(store, db_url, fake_storage):
    fake_storage.fail_upload = 413
    with pytest.raises(StoreError, match="50 MB"):
        ab.run(store)
    with store.db.connect() as conn:
        last = ab.last(conn)
        _, checks = system_status.operations(conn, store.db.tz)
    assert not last["ok"] and "50 MB" in last["error"]
    assert next(c for c in checks if c["label"] == "สำรองข้อมูล")["status"] == "bad"

    timer = ab.BackupTimer(store)
    night = datetime.now(store.db.tz).replace(hour=3, minute=10)
    assert timer.tick(night.replace(hour=2)) is None and fake_storage.uploads == 1  # before the hour: nothing
    for _ in range(4):
        assert timer.tick(night) is None
    assert fake_storage.uploads == 4  # three tries tonight, then it waits for tomorrow
    fake_storage.fail_upload = None
    tomorrow = night + timedelta(days=1)
    assert timer.tick(tomorrow)["ok"] and timer.tick(tomorrow) is None  # once a night
    assert _rows(db_url, "SELECT value FROM app_settings WHERE key = %s", f"backup_run:{tomorrow.date().isoformat()}") == [("done",)]


@needs_db
def test_wrong_key_and_public_bucket_are_refused(store, fake_storage, monkeypatch):
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "not-the-key")
    with pytest.raises(StoreError, match="SUPABASE_SERVICE_ROLE_KEY"):
        ab.run(store)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", KEY)
    fake_storage.buckets["orchestra-backups"] = {"public": True, "objects": {}}
    with pytest.raises(StoreError, match="public"):
        ab.run(store)
    assert fake_storage.uploads == 0
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY")
    assert ab.BackupTimer(store).tick() is None  # not set up: the timer does nothing
    with pytest.raises(StoreError, match="BACKUP_ENCRYPTION_KEY"):
        ab.run(store)


@needs_db
def test_admin_page_runs_lists_and_downloads(store, db_url, fake_storage, owner, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "access", privacy.AccessLog(store))
    view = owner.get("/api/system/backups").json()
    assert view["config"]["configured"] and view["files"] == [] and view["last"] is None
    assert PASSPHRASE not in json.dumps(view) and KEY not in json.dumps(view)
    assert owner.post("/api/system/backups/run", json={}).status_code == 400  # confirm first
    ran = owner.post("/api/system/backups/run", json={"confirm": True})
    assert ran.status_code == 200, ran.text
    name = ran.json()["result"]["name"]
    assert [f["name"] for f in ran.json()["files"]] == [name] and ran.json()["result"]["by"].startswith("ผู้ดูแลระบบหลัก")
    got = owner.get(f"/api/system/backups/{name}")
    assert got.status_code == 200 and got.content.startswith(ab.MAGIC)
    assert owner.get("/api/system/backups/..%2F..%2Fetc%2Fpasswd").status_code == 404
    scheduler = make_user(owner, "sched3", "scheduler")
    assert scheduler.get("/api/system/backups").status_code == 403
    assert scheduler.get(f"/api/system/backups/{name}").status_code == 403
    rows = {r[0] for r in _rows(db_url, "SELECT username FROM patient_access_log WHERE action = 'backup'")}
    assert rows == {OWNER[0]}  # the run and the download, by the person who did them


def test_demo_mode_says_it_needs_supabase(owner, monkeypatch):
    for var in ("BACKUP_ENCRYPTION_KEY", "SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_URL"):
        monkeypatch.delenv(var, raising=False)
    view = owner.get("/api/system/backups").json()
    assert view["supabase"] is False and not view["config"]["configured"]
    assert owner.post("/api/system/backups/run", json={"confirm": True}).status_code == 400
    row = next(r for r in owner.get("/api/system/status").json()["settings"] if r["key"].startswith("BACKUP_ENCRYPTION_KEY"))
    assert not row["ok"] and "Supabase" in row["value"]
