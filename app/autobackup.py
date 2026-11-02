"""Automatic backup every night: the full backup ZIP (``app/backup.py``), encrypted, kept outside the database.

Supabase's free plan keeps no backups, and a backup someone must remember to download is soon forgotten.
Each night (``BACKUP_HOUR``, default 03:00, after the PDPA run at 02:00) the server:

1. writes the backup ZIP (every table, HN and names included);
2. encrypts it with a passphrase only the hospital holds (``BACKUP_ENCRYPTION_KEY``): AES-256-GCM, key
   from scrypt, in 1 MB chunks so a cut-off, reordered or changed file is refused, never half-restored;
3. uploads it to a private Supabase Storage bucket (``orchestra-backups``) with the project's
   service-role key (``SUPABASE_SERVICE_ROLE_KEY``);
4. removes files older than ``BACKUP_KEEP_DAYS`` (default 14), always keeping the newest 3.

Without the passphrase the files cannot be read, so it must be kept somewhere else as well (a password
manager or a sealed envelope with the hospital's IT): Render going away must not take it with it.
Restore: ``python -m app.backup decrypt <file>.orcbak backup.zip`` then ``restore`` as usual (or
``restore`` the .orcbak directly), with ``BACKUP_ENCRYPTION_KEY`` set.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

from .store import StoreError

log = logging.getLogger("orchestra.backup")

MAGIC = b"ORCBAK1\n"
SALT_LEN, PREFIX_LEN = 16, 7
CHUNK = 1 << 20
MIN_PASSPHRASE = 16
MIN_KEEP = 3                 # files kept whatever their age (so failing nights never empty the bucket)
ATTEMPTS = 3                 # tries per night (then the next night)
NAME = re.compile(r"^orchestra-(\d{4}-\d{2}-\d{2})-(\d{4})\.orcbak$")
SYSTEM_ACTOR = {"name": "สำรองอัตโนมัติ", "role": "ระบบ"}


# ------------------------------------------------------------------ encryption

def _key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
    return prefix + counter.to_bytes(4, "big") + (b"\x01" if last else b"\x00")


def encrypt(src, dst, passphrase: str) -> int:
    """Encrypt ``src`` (file) into ``dst``; returns the bytes written. Each chunk is authenticated with its
    number and whether it is the last, and every chunk with the file's header."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, prefix = secrets.token_bytes(SALT_LEN), secrets.token_bytes(PREFIX_LEN)
    header = MAGIC + salt + prefix
    aes = AESGCM(_key(passphrase, salt))
    written = dst.write(header)
    counter, chunk = 0, src.read(CHUNK)
    while True:
        following = src.read(CHUNK)
        last = not following
        sealed = aes.encrypt(_nonce(prefix, counter, last), chunk, header)
        written += dst.write(len(sealed).to_bytes(4, "big") + sealed)
        if last:
            return written
        chunk, counter = following, counter + 1


def decrypt(src, dst, passphrase: str) -> int:
    """The other way; raises ``StoreError`` for a wrong passphrase or a damaged, cut-off or altered file."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    header = src.read(len(MAGIC) + SALT_LEN + PREFIX_LEN)
    if not header.startswith(MAGIC) or len(header) < len(MAGIC) + SALT_LEN + PREFIX_LEN:
        raise StoreError("ไม่ใช่ไฟล์สำรองข้อมูลที่เข้ารหัสของ ORchestraAI (.orcbak)")
    salt, prefix = header[len(MAGIC):len(MAGIC) + SALT_LEN], header[len(MAGIC) + SALT_LEN:]
    aes = AESGCM(_key(passphrase, salt))
    counter, written, size = 0, 0, src.read(4)
    while True:
        if len(size) < 4:
            raise StoreError("ไฟล์สำรองไม่ครบ (ถูกตัดหรือดาวน์โหลดไม่จบ)")
        sealed = src.read(int.from_bytes(size, "big"))
        if len(sealed) != int.from_bytes(size, "big"):
            raise StoreError("ไฟล์สำรองไม่ครบ (ถูกตัดหรือดาวน์โหลดไม่จบ)")
        size = src.read(4)
        last = not size
        try:
            written += dst.write(aes.decrypt(_nonce(prefix, counter, last), sealed, header))
        except InvalidTag as exc:
            raise StoreError("ถอดรหัสไม่ได้: รหัสผ่านสำรองข้อมูล (BACKUP_ENCRYPTION_KEY) ไม่ถูกต้อง "
                             "หรือไฟล์เสียหาย/ไม่ครบ") from exc
        if last:
            return written
        counter += 1


def is_encrypted(path_or_file) -> bool:
    if hasattr(path_or_file, "read"):
        start = path_or_file.read(len(MAGIC))
        path_or_file.seek(0)
        return start == MAGIC
    with open(path_or_file, "rb") as f:
        return f.read(len(MAGIC)) == MAGIC


# ------------------------------------------------------------------ settings

def project_url(database_url: str | None) -> str | None:
    """``https://<ref>.supabase.co`` from the database address (direct ``db.<ref>.supabase.co`` or the
    pooler's user ``postgres.<ref>``), unless ``SUPABASE_URL`` says otherwise."""
    explicit = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    if not database_url:
        return None
    try:
        parts = urlparse(database_url)
        host, user = parts.hostname or "", unquote(parts.username or "")
    except ValueError:
        return None
    match = re.fullmatch(r"db\.([a-z0-9]{10,40})\.supabase\.co", host) or re.fullmatch(r"postgres\.([a-z0-9]{10,40})", user)
    return f"https://{match.group(1)}.supabase.co" if match else None


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return min(max(int(os.environ.get(name, default)), low), high)
    except ValueError:
        return default


@dataclass
class Config:
    passphrase: str
    service_key: str
    url: str | None
    bucket: str
    hour: int
    keep_days: int

    @classmethod
    def from_env(cls, database_url: str | None) -> "Config":
        return cls(os.environ.get("BACKUP_ENCRYPTION_KEY", ""), (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip(),
                   project_url(database_url), (os.environ.get("BACKUP_BUCKET") or "orchestra-backups").strip(),
                   _int_env("BACKUP_HOUR", 3, 0, 23), _int_env("BACKUP_KEEP_DAYS", 14, 3, 365))

    def missing(self) -> list[str]:
        out = []
        if not self.passphrase:
            out.append("BACKUP_ENCRYPTION_KEY (รหัสผ่านสำหรับเข้ารหัสไฟล์สำรอง)")
        elif len(self.passphrase) < MIN_PASSPHRASE:
            out.append(f"BACKUP_ENCRYPTION_KEY ต้องยาวอย่างน้อย {MIN_PASSPHRASE} ตัวอักษร")
        if not self.service_key:
            out.append("SUPABASE_SERVICE_ROLE_KEY (คีย์ service_role ของโปรเจกต์ Supabase)")
        if not self.url:
            out.append("SUPABASE_URL (หาที่อยู่โปรเจกต์จาก DATABASE_URL ไม่ได้)")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,62}", self.bucket):
            out.append("BACKUP_BUCKET (ชื่อ bucket ไม่ถูกต้อง)")
        return out

    @property
    def ready(self) -> bool:
        return not self.missing()

    def public(self) -> dict:
        """For the status page: never the passphrase or the key."""
        return {"configured": self.ready, "missing": self.missing(), "bucket": self.bucket, "hour": self.hour,
                "keep_days": self.keep_days, "project": self.url}


# ------------------------------------------------------------------ Supabase Storage (REST)

class Storage:
    """The few Storage calls a backup needs. The bucket is private: files go out only through this server."""

    def __init__(self, url: str, key: str, bucket: str, *, timeout: float = 120.0) -> None:
        self.base, self.key, self.bucket, self.timeout = url.rstrip("/") + "/storage/v1", key, bucket, timeout

    def _call(self, method: str, path: str, *, body: dict | None = None, data=None, length: int | None = None,
              headers: dict | None = None, stream: bool = False):
        heads = {"Authorization": f"Bearer {self.key}", "apikey": self.key, **(headers or {})}
        payload = data
        if body is not None:
            payload = json.dumps(body).encode()
            heads["Content-Type"] = "application/json"
        if length is not None:
            heads["Content-Length"] = str(length)
        request = urllib.request.Request(self.base + path, data=payload, method=method, headers=heads)
        try:
            response = urllib.request.urlopen(request, timeout=self.timeout)  # noqa: S310 (https URL from settings)
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise StorageError(exc.code, detail) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise StoreError(f"ติดต่อ Supabase Storage ไม่ได้: {getattr(exc, 'reason', exc)}") from exc
        if stream:
            return response
        with response:
            raw = response.read()
        return json.loads(raw) if raw and response.headers.get("Content-Type", "").startswith("application/json") else raw

    def ensure_bucket(self) -> None:
        """Make the private bucket the first time; refuse a public one (anyone with the link could download)."""
        try:
            found = self._call("GET", f"/bucket/{quote(self.bucket)}")
        except StorageError as exc:
            if exc.status not in (400, 404):
                raise
            try:
                self._call("POST", "/bucket", body={"id": self.bucket, "name": self.bucket, "public": False})
            except StorageError as again:
                if again.status != 409 and "already exists" not in again.detail.lower():
                    raise
            return
        if isinstance(found, dict) and found.get("public"):
            raise StoreError(f"bucket {self.bucket} ใน Supabase เป็น public (ใครมีลิงก์ก็ดาวน์โหลดได้) "
                             "ให้ปิด public ก่อน หรือใช้ชื่อ bucket ใหม่ (BACKUP_BUCKET)")

    def upload(self, name: str, fileobj, size: int) -> None:
        self._call("POST", f"/object/{quote(self.bucket)}/{quote(name)}", data=fileobj, length=size,
                   headers={"Content-Type": "application/octet-stream", "x-upsert": "true"})

    def files(self) -> list[dict]:
        rows = self._call("POST", f"/object/list/{quote(self.bucket)}",
                          body={"prefix": "", "limit": 1000, "offset": 0, "sortBy": {"column": "name", "order": "desc"}})
        out = []
        for r in rows if isinstance(rows, list) else []:
            if NAME.match(r.get("name") or ""):
                out.append({"name": r["name"], "size": int((r.get("metadata") or {}).get("size") or 0),
                            "at": r.get("created_at")})
        return sorted(out, key=lambda f: f["name"], reverse=True)

    def delete(self, names: list[str]) -> None:
        if names:
            self._call("DELETE", f"/object/{quote(self.bucket)}", body={"prefixes": names})

    def download(self, name: str):
        """The open response (read it in chunks)."""
        return self._call("GET", f"/object/{quote(self.bucket)}/{quote(name)}", stream=True)


class StorageError(StoreError):
    def __init__(self, status: int, detail: str) -> None:
        self.status, self.detail = status, detail
        hint = {401: "คีย์ไม่ถูกต้อง (SUPABASE_SERVICE_ROLE_KEY)", 403: "คีย์ไม่มีสิทธิ์ (ต้องเป็น service_role)",
                413: "ไฟล์ใหญ่เกินที่ Supabase Storage รับ (แผนฟรีไฟล์ละไม่เกิน 50 MB)"}.get(status, "")
        super().__init__(f"Supabase Storage ตอบ {status}" + (f": {hint}" if hint else ""))


# ------------------------------------------------------------------ one run

def _now(store) -> datetime:
    return datetime.now(store.db.tz)


def file_name(at: datetime) -> str:
    return f"orchestra-{at:%Y-%m-%d}-{at:%H%M}.orcbak"


def to_remove(files: list[dict], today: date, keep_days: int) -> list[str]:
    """Older than ``keep_days``, but never the newest ``MIN_KEEP``."""
    names = sorted((f["name"] for f in files if NAME.match(f["name"])), reverse=True)
    cutoff = today - timedelta(days=keep_days)
    return [n for n in names[MIN_KEEP:] if date.fromisoformat(NAME.match(n).group(1)) < cutoff]


def run(store, access=None, *, actor: dict | None = None, config: Config | None = None) -> dict:
    """Back up, encrypt, upload, check it arrived whole, prune; the outcome is saved for the status page."""
    from . import backup, system_status

    config = config or Config.from_env(store.db.url)
    if not config.ready:
        raise StoreError("ยังไม่ได้ตั้งค่าสำรองข้อมูลอัตโนมัติ: " + " · ".join(config.missing()))
    at = _now(store)
    name = file_name(at)
    storage = Storage(config.url, config.service_key, config.bucket)
    try:
        out, manifest = backup.dump(store.db.url)
        with out, tempfile.TemporaryFile() as sealed:
            size = encrypt(out, sealed, config.passphrase)
            sealed.seek(0)
            storage.ensure_bucket()
            storage.upload(name, sealed, size)
        files = storage.files()
        stored = next((f for f in files if f["name"] == name), None)
        if stored is None or (stored["size"] and stored["size"] != size):
            raise StoreError("อัปโหลดแล้วแต่ไฟล์ใน Supabase Storage ไม่ครบ")
        old = to_remove(files, at.date(), config.keep_days)
        storage.delete(old)
    except Exception as exc:
        message = str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
        _save(store, {"at": at.isoformat(timespec="minutes"), "ok": False, "error": message,
                      "by": _who(actor)})
        log.error("automatic backup failed: %s", message)  # also listed for the admin (ErrorLogHandler)
        raise StoreError(f"สำรองข้อมูลอัตโนมัติไม่สำเร็จ: {message}") from exc
    rows = sum(t["rows"] for t in manifest["tables"])
    result = {"at": at.isoformat(timespec="minutes"), "ok": True, "name": name, "size": size, "rows": rows,
              "tables": len(manifest["tables"]), "removed": len(old), "kept": len(files) - len(old), "by": _who(actor)}
    _save(store, result)
    system_status.note_backup(store, manifest, size, actor or SYSTEM_ACTOR)
    if access is not None:  # every patient's identity is in the file (encrypted): on the PDPA log too
        from .auth import User

        who = User("system:backup", "system", "สำรองข้อมูลอัตโนมัติ", "system") if actor is None else \
            User(actor.get("user_id") or actor.get("username") or "-", actor.get("username") or "-",
                 actor.get("name") or "-", actor.get("role") or "-")
        access.record(who, "backup", None, [], None, patients=backup.patients_in(manifest))
    return result


def _who(actor: dict | None) -> str:
    return "ระบบ (ทุกคืน)" if actor is None else f"{actor.get('name')} ({actor.get('role')})"


def _save(store, result: dict) -> None:
    from . import system_status

    system_status._set(store, "backup_auto_last", json.dumps(result, ensure_ascii=False))


def last(conn) -> dict | None:
    if not conn.execute("SELECT to_regclass('public.app_settings') IS NOT NULL AS ok").fetchone()["ok"]:
        return None
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'backup_auto_last'").fetchone()
    return json.loads(row["value"]) if row else None


# ------------------------------------------------------------------ every night

class BackupTimer:
    """From ``hour`` local time, once a day (claimed in app_settings so restarts or a second server do not
    repeat it); a failed night is tried again ``ATTEMPTS`` times, 15 minutes apart."""

    def __init__(self, store, access=None, *, every_s: float = 900.0) -> None:
        self.store, self.access, self.every_s = store, access, every_s
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="backup", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        wait = 120.0
        while not self._stop.wait(wait):
            self.tick()
            wait = self.every_s

    def tick(self, now: datetime | None = None) -> dict | None:
        try:
            if getattr(self.store, "mode", "demo") != "supabase":
                return None
            config = Config.from_env(self.store.db.url)
            if not config.ready:
                return None
            now = now or _now(self.store)
            if now.hour < config.hour:
                return None
            key = f"backup_run:{now.date().isoformat()}"
            with self.store.db.connect() as conn:
                if not conn.execute("SELECT to_regclass('public.app_settings') IS NOT NULL AS ok").fetchone()["ok"]:
                    return None
                claimed = conn.execute(
                    """INSERT INTO app_settings (key, value) VALUES (%s, '1')
                       ON CONFLICT (key) DO UPDATE SET value = (app_settings.value::int + 1)::text
                       WHERE CASE WHEN app_settings.value ~ '^[0-9]+$' THEN app_settings.value::int < %s ELSE false END
                       RETURNING value""", (key, ATTEMPTS)).fetchone()
            if claimed is None:
                return None  # done today, or tried enough
            result = run(self.store, self.access, config=config)
            from . import system_status

            system_status._set(self.store, key, "done")
            system_status.mark_job(self.store, "backup", now.astimezone(timezone.utc))
            return result
        except StoreError:
            return None  # saved and logged by run()
        except Exception:  # never take the server down
            log.exception("automatic backup run failed")
            return None
