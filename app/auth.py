"""User accounts, roles and sessions.

- Passwords: scrypt (salted), never stored in plain text.
- Sessions: HMAC-signed cookie {user id, session version, expiry}; bumping
  the user's session_version (password change, disable) signs them out.
- The account from APP_USERNAME / APP_PASSWORD always works as the owner's
  admin account, so the first real accounts can be created from the app.
- Accounts live in ``app_users`` (migration 003) in Supabase mode, or in
  memory in demo mode.
- Two-step sign-in (migration 018, ``app/twofactor.py``): a 6-digit code from an
  authenticator app after the password. Required for the roles in
  ``TWO_FACTOR_ROLES`` (default admin) once the SQL file has run; anyone may switch
  it on. The owner account uses ``APP_TOTP_SECRET`` from the environment, so it
  still signs in when the database is down.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import lru_cache

from . import twofactor as tf

ROLES = {
    "admin": "ผู้ดูแลระบบ",
    "scheduler": "ผู้จัดตาราง OR (หัวหน้าพยาบาล)",
    "surgeon": "ศัลยแพทย์",
    "cssd": "เจ้าหน้าที่จ่ายกลาง (CSSD)",
    "staff": "วิสัญญี / พยาบาล",
}

# What each role may do. The UI hides what is not allowed; the API enforces it.
PERMISSIONS = {
    "view": {"admin", "scheduler", "surgeon", "cssd", "staff"},
    "book": {"admin", "scheduler", "surgeon"},  # surgeons: own cases only
    "emergency": {"admin", "scheduler", "surgeon"},
    "overrun": {"admin", "scheduler", "surgeon"},
    "equipment": {"admin", "scheduler", "cssd"},
    "record_times": {"admin", "scheduler", "surgeon", "staff"},  # surgeon / staff: cases they are on
    "patients": {"admin", "scheduler", "surgeon", "staff"},  # see patient HN / name
    "all_notifications": {"admin", "scheduler"},
    "staff_view": {"admin", "scheduler", "surgeon", "staff"},  # Staffing tab (clinical staff: own row)
    "staff_all": {"admin", "scheduler"},  # everyone's hours and fatigue
    "hospital_view": {"admin", "scheduler"},  # hospital data pages (read)
    "hospital_data": {"admin"},  # rooms, staff records, equipment types and units
    "roster": {"admin", "scheduler"},  # shifts, leave, room closures, re-plan a day
    "inventory_view": {"admin", "scheduler", "cssd"},  # supplies and implants stock
    "inventory": {"admin", "cssd"},  # receive, count, order supplies; what procedures use
    "reports": {"admin", "scheduler"},  # management reports
    "cards": {"admin", "scheduler", "surgeon"},  # surgeon preference cards (surgeons: their own)
    "readiness": {"admin", "scheduler", "surgeon", "staff"},  # pre-op checklist (surgeons: their own cases)
    "users": {"admin"},
    "seed": {"admin"},
    "reset": {"admin"},
}
STAFF_LINKED_ROLES = {"surgeon", "staff"}
SESSION_HOURS = 12
COOKIE = "orchestra_session"
MIN_PASSWORD = 10
ENV_ADMIN_ID = "env-admin"


class AuthError(Exception):
    """Login or account change refused; the message is shown to the user."""


class TooManyAttempts(AuthError):
    pass


class ServiceUnavailable(AuthError):
    """The account store (database) cannot be reached right now."""


DB_DOWN = ("ระบบเชื่อมต่อฐานข้อมูลไม่ได้ชั่วคราว ผู้ดูแลระบบ: เข้าด้วยบัญชีหลัก (APP_USERNAME) "
           "เพื่อดูสาเหตุ หรือดู Render → Logs")


NO_TABLE = "ยังไม่มีตาราง app_users ให้รัน supabase/migrations/20261102000000_user_accounts.sql ใน SQL Editor"
NEED_2FA_SQL = "ยังใช้การยืนยันตัวตน 2 ชั้นไม่ได้: ต้องรัน SQL ไฟล์ 018 ก่อน (หน้า ⚙ สถานะระบบ → รัน SQL ที่ยังขาด)"
PENDING_S = 300           # between the password and the code
SETUP_S = 900             # to scan the QR code and type the first code


class CodeNeeded(AuthError):
    """The password was right; the 6-digit code comes next (``pending`` goes back with it)."""

    def __init__(self, user: "User", pending: str) -> None:
        super().__init__("กรอกรหัส 6 หลักจากแอปยืนยันตัวตน")
        self.user, self.pending = user, pending


def required_roles() -> set[str]:
    """Roles that must use two-step sign-in (``TWO_FACTOR_ROLES``: comma list, or ``none``)."""
    raw = os.environ.get("TWO_FACTOR_ROLES")
    if raw is None:
        return {"admin"}
    return {r.strip() for r in raw.split(",") if r.strip() in ROLES}


def env_totp_secret() -> str | None:
    secret = (os.environ.get("APP_TOTP_SECRET") or "").strip().upper().replace(" ", "")
    return secret if tf.valid_secret(secret) else None


def can(role: str, permission: str) -> bool:
    return role in PERMISSIONS.get(permission, set())


# ------------------------------------------------------------------ passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # checked when there is no real hash: every login costs one scrypt


def check_new_password(password: str, username: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PASSWORD} ตัวอักษร")
    if username and username.lower() in password.lower():
        raise AuthError("รหัสผ่านต้องไม่มีชื่อผู้ใช้อยู่ข้างใน")


# ------------------------------------------------------------------ sessions

class Signer:
    def __init__(self, secret: bytes) -> None:
        self.secret = secret

    @classmethod
    def from_env(cls) -> "Signer":
        explicit = os.environ.get("SESSION_SECRET")
        if explicit:
            return cls(explicit.encode())
        material = os.environ.get("APP_PASSWORD", "") + "|" + os.environ.get("DATABASE_URL", "")
        if material != "|":  # stable across restarts without another setting
            # A slow derivation: a cookie must not let anyone test password guesses quickly offline.
            return cls(hashlib.scrypt(material.encode(), salt=b"orchestra-session-v2", n=2**15, r=8, p=1,
                                      maxmem=64 * 1024 * 1024, dklen=32))
        return cls(secrets.token_bytes(32))

    def sign(self, payload: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        mac = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{mac}"

    def verify(self, token: str) -> dict | None:
        try:
            body, mac = token.rsplit(".", 1)
        except ValueError:
            return None
        expected = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        if payload.get("exp", 0) < time.time():
            return None
        return payload


class LoginLimiter:
    """At most 5 failed logins per user name + address, and 20 per user name from any address, in 10 minutes.

    The per-name cap stops guessing spread over many addresses (or a forged X-Forwarded-For).
    """

    def __init__(self, limit: int = 5, window_s: int = 600, per_user: int = 20) -> None:
        self.limit, self.window_s, self.per_user = limit, window_s, per_user
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: tuple[str, str]) -> list[float]:
        now = time.time()
        fails = [t for t in self._fails.get(key, []) if now - t < self.window_s]
        if fails:
            self._fails[key] = fails
        else:
            self._fails.pop(key, None)
        return fails

    def blocked(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.limit or len(self._recent((key[0], "*"))) >= self.per_user

    def fail(self, key: tuple[str, str]) -> None:
        with self._lock:
            now = time.time()
            for k in (key, (key[0], "*")):
                self._fails[k] = self._recent(k) + [now]
            if len(self._fails) > 20000:  # forget stale entries so the table cannot grow without end
                for k in list(self._fails):
                    self._recent(k)

    def clear(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._fails.pop(key, None)


# ------------------------------------------------------------------- accounts

@dataclass
class User:
    user_id: str
    username: str
    display_name: str
    role: str
    staff_id: str | None = None
    is_active: bool = True
    must_change_password: bool = False
    session_version: int = 1
    password_hash: str = ""
    last_login_at: str | None = None
    totp_enabled: bool = False
    two_factor_required: bool = False  # this role must use it (and migration 018 has run)

    @property
    def must_setup_2fa(self) -> bool:
        return self.two_factor_required and not self.totp_enabled

    def public(self) -> dict:
        data = asdict(self)
        data.pop("password_hash")
        data["role_label"] = ROLES.get(self.role, self.role)
        data["permissions"] = sorted(p for p in PERMISSIONS if can(self.role, p))
        data["must_setup_2fa"] = self.must_setup_2fa
        data["owner_account"] = self.user_id == ENV_ADMIN_ID
        return data


@lru_cache(maxsize=4)
def _admin_version(password: str) -> int:
    """Session version of the owner admin: changes with APP_PASSWORD and, being slow and salted, tells
    someone holding the cookie nothing useful about the password."""
    digest = hashlib.scrypt(password.encode(), salt=b"orchestra-admin-version", n=2**14, r=8, p=1, dklen=8)
    return int.from_bytes(digest[:3], "big")


def env_admin() -> User | None:
    password = os.environ.get("APP_PASSWORD", "")
    if not password:
        return None
    secret = env_totp_secret()
    version = _admin_version(password + "|" + (secret or ""))  # new password or code secret -> old sessions end
    return User(ENV_ADMIN_ID, os.environ.get("APP_USERNAME", "orchestra").lower(),
                "ผู้ดูแลระบบหลัก (Render)", "admin", session_version=version, totp_enabled=secret is not None)


def _validate(role: str, display_name: str, staff_id: str | None) -> None:
    if role not in ROLES:
        raise AuthError(f"บทบาทไม่ถูกต้อง: {role}")
    if not display_name.strip():
        raise AuthError("กรุณากรอกชื่อที่แสดง")
    if role == "surgeon" and not staff_id:
        raise AuthError("บัญชีศัลยแพทย์ต้องผูกกับรายชื่อศัลยแพทย์ในระบบ")


class MemoryUserStore:
    """Demo mode: accounts kept in memory (gone after a restart)."""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._totp: dict[str, dict] = {}
        self._lock = threading.Lock()

    def ready(self) -> bool:
        return True

    def two_factor_ready(self) -> bool:
        return True  # available to try; required only with a database (real accounts)

    def totp_state(self, user_id: str) -> dict | None:
        state = self._totp.get(user_id)
        return dict(state, recovery=list(state["recovery"])) if state else None

    def use_step(self, user_id: str, step: int) -> bool:
        with self._lock:
            state = self._totp.get(user_id)
            if not state or (state["last_step"] is not None and state["last_step"] >= step):
                return False
            state["last_step"] = step
            return True

    def use_recovery(self, user_id: str, hashed: str) -> int | None:
        with self._lock:
            state = self._totp.get(user_id)
            if not state or hashed not in state["recovery"]:
                return None
            state["recovery"].remove(hashed)
            return len(state["recovery"])

    def enable_totp(self, user_id: str, secret: str, step: int, recovery: list[str]) -> None:
        with self._lock:
            self._totp[user_id] = {"secret": secret, "last_step": step, "recovery": list(recovery)}
            self._users[user_id].totp_enabled = True
            self._users[user_id].session_version += 1

    def set_recovery(self, user_id: str, recovery: list[str]) -> None:
        with self._lock:
            if user_id in self._totp:
                self._totp[user_id]["recovery"] = list(recovery)

    def disable_totp(self, user_id: str) -> None:
        with self._lock:
            self._totp.pop(user_id, None)
            if user_id in self._users:
                self._users[user_id].totp_enabled = False
                self._users[user_id].session_version += 1

    def get(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def by_username(self, username: str) -> User | None:
        return next((u for u in self._users.values() if u.username == username.lower()), None)

    def list(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: u.username)

    def create(self, username: str, display_name: str, role: str, staff_id: str | None, password: str) -> User:
        username = username.strip().lower()
        _validate(role, display_name, staff_id)
        check_new_password(password, username)
        with self._lock:
            if self.by_username(username) or username == (env_admin() or User("", "", "", "")).username:
                raise AuthError(f"มีชื่อผู้ใช้ {username} แล้ว")
            user = User(str(uuid.uuid4()), username, display_name.strip(), role, staff_id,
                        must_change_password=True, password_hash=hash_password(password))
            self._users[user.user_id] = user
            return user

    def update(self, user_id: str, **changes) -> User:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                raise AuthError("ไม่พบผู้ใช้")
            merged = {**asdict(user), **{k: v for k, v in changes.items() if v is not None or k == "staff_id"}}
            _validate(merged["role"], merged["display_name"], merged["staff_id"])
            for key in ("display_name", "role", "staff_id", "is_active"):
                setattr(user, key, merged[key])
            user.session_version += 1  # role or status changed: sign out everywhere
            return user

    def set_password(self, user_id: str, password: str, *, temporary: bool) -> None:
        with self._lock:
            user = self._users[user_id]
            check_new_password(password, user.username)
            user.password_hash = hash_password(password)
            user.must_change_password = temporary
            user.session_version += 1

    def end_sessions(self, user_id: str) -> None:
        with self._lock:
            if user_id in self._users:
                self._users[user_id].session_version += 1

    def touch_login(self, user_id: str) -> None:
        if user_id in self._users:
            self._users[user_id].last_login_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class DbUserStore:
    """Supabase mode: accounts in ``app_users`` (migration 003)."""

    COLS = ("user_id, username, display_name, role, staff_id, is_active, must_change_password, "
            "session_version, password_hash, last_login_at")

    def __init__(self, database) -> None:
        self.db = database
        self._2fa_checked = (False, 0.0)  # (columns found, when asked)

    def two_factor_ready(self) -> bool:
        """Migration 018 has run (once found it is not asked again; until then at most once a minute)."""
        found, at = self._2fa_checked
        if found or time.monotonic() - at < 60:
            return found
        rows = self._query("""SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
                                             AND table_name = 'app_users' AND column_name = 'totp_secret') AS ok""",
                           missing=[{"ok": False}])
        found = bool(rows and rows[0]["ok"])
        self._2fa_checked = (found, time.monotonic())
        return found

    def _cols(self) -> str:
        return self.COLS + (", totp_secret IS NOT NULL AS totp_on" if self.two_factor_ready() else "")

    def _need_2fa(self) -> None:
        if not self.two_factor_ready():
            raise AuthError(NEED_2FA_SQL)

    def totp_state(self, user_id: str) -> dict | None:
        self._need_2fa()
        rows = self._query("SELECT totp_secret, totp_last_step, recovery_codes FROM app_users WHERE user_id = %s",
                           (uuid.UUID(user_id),))
        if not rows or not rows[0]["totp_secret"]:
            return None
        return {"secret": rows[0]["totp_secret"], "last_step": rows[0]["totp_last_step"],
                "recovery": list(rows[0]["recovery_codes"] or [])}

    def use_step(self, user_id: str, step: int) -> bool:
        """Record the code's step; False when it (or a later one) was used already (two sign-ins at once)."""
        rows = self._query("""UPDATE app_users SET totp_last_step = %s WHERE user_id = %s AND totp_secret IS NOT NULL
                              AND (totp_last_step IS NULL OR totp_last_step < %s) RETURNING 1 AS ok""",
                           (step, uuid.UUID(user_id), step))
        return bool(rows)

    def use_recovery(self, user_id: str, hashed: str) -> int | None:
        rows = self._query("""UPDATE app_users SET recovery_codes = recovery_codes - %s::text
                              WHERE user_id = %s AND totp_secret IS NOT NULL AND recovery_codes ? %s::text
                              RETURNING jsonb_array_length(recovery_codes) AS left_""",
                           (hashed, uuid.UUID(user_id), hashed))
        return rows[0]["left_"] if rows else None

    def enable_totp(self, user_id: str, secret: str, step: int, recovery: list[str]) -> None:
        self._need_2fa()
        from psycopg.types.json import Jsonb

        self._query("""UPDATE app_users SET totp_secret = %s, totp_enabled_at = NOW(), totp_last_step = %s,
                              recovery_codes = %s, session_version = session_version + 1
                       WHERE user_id = %s RETURNING 1 AS ok""", (secret, step, Jsonb(recovery), uuid.UUID(user_id)))

    def set_recovery(self, user_id: str, recovery: list[str]) -> None:
        from psycopg.types.json import Jsonb

        self._query("UPDATE app_users SET recovery_codes = %s WHERE user_id = %s RETURNING 1 AS ok",
                    (Jsonb(recovery), uuid.UUID(user_id)))

    def disable_totp(self, user_id: str) -> None:
        self._need_2fa()
        self._query("""UPDATE app_users SET totp_secret = NULL, totp_enabled_at = NULL, totp_last_step = NULL,
                              recovery_codes = NULL, session_version = session_version + 1
                       WHERE user_id = %s RETURNING 1 AS ok""", (uuid.UUID(user_id),))

    @contextmanager
    def _connect(self):
        from .db import DatabaseUnavailable

        try:
            with self.db.connect() as conn:
                yield conn
        except DatabaseUnavailable as exc:
            raise ServiceUnavailable(DB_DOWN) from exc

    def _query(self, sql: str, params=(), *, missing=None):
        """Run a query; if migration 003 is missing return ``missing`` (or explain)."""
        import psycopg

        from .db import DatabaseUnavailable

        try:
            with self.db.connect() as conn:
                return list(conn.execute(sql, params))
        except DatabaseUnavailable as exc:
            raise ServiceUnavailable(DB_DOWN) from exc
        except psycopg.errors.UndefinedTable as exc:
            if missing is not None:
                return missing
            raise AuthError(NO_TABLE) from exc

    @staticmethod
    def _user(row) -> User | None:
        if row is None:
            return None
        ready = "totp_on" in row  # migration 018 has run
        return User(
            str(row["user_id"]), row["username"], row["display_name"], row["role"],
            str(row["staff_id"]) if row["staff_id"] else None, row["is_active"], row["must_change_password"],
            row["session_version"], row["password_hash"],
            row["last_login_at"].isoformat() if row["last_login_at"] else None,
            totp_enabled=bool(row.get("totp_on")), two_factor_required=ready and row["role"] in required_roles(),
        )

    def ready(self) -> bool:
        return self._query("SELECT to_regclass('public.app_users') AS t")[0]["t"] is not None

    def get(self, user_id: str) -> User | None:
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            return None
        rows = self._query(f"SELECT {self._cols()} FROM app_users WHERE user_id = %s", (uid,), missing=[])
        return self._user(rows[0]) if rows else None

    def by_username(self, username: str) -> User | None:
        rows = self._query(f"SELECT {self._cols()} FROM app_users WHERE username = %s",
                           (username.strip().lower(),), missing=[])
        return self._user(rows[0]) if rows else None

    def list(self) -> list[User]:
        return [self._user(r) for r in self._query(f"SELECT {self._cols()} FROM app_users ORDER BY username")]

    def create(self, username: str, display_name: str, role: str, staff_id: str | None, password: str) -> User:
        username = username.strip().lower()
        _validate(role, display_name, staff_id)
        check_new_password(password, username)
        admin = env_admin()
        if admin and username == admin.username:
            raise AuthError(f"มีชื่อผู้ใช้ {username} แล้ว")
        import psycopg

        try:
            with self._connect() as conn:
                row = conn.execute(
                    f"""INSERT INTO app_users (username, display_name, role, staff_id, password_hash)
                        VALUES (%s, %s, %s, %s, %s) RETURNING {self._cols()}""",
                    (username, display_name.strip(), role, uuid.UUID(staff_id) if staff_id else None,
                     hash_password(password)),
                ).fetchone()
        except psycopg.errors.UndefinedTable as exc:
            raise AuthError(NO_TABLE) from exc
        except psycopg.errors.UniqueViolation as exc:
            raise AuthError(f"มีชื่อผู้ใช้ {username} แล้ว") from exc
        except psycopg.errors.ForeignKeyViolation as exc:
            raise AuthError("ไม่พบรายชื่อบุคลากรที่เลือก") from exc
        return self._user(row)

    def update(self, user_id: str, **changes) -> User:
        current = self.get(user_id)
        if current is None:
            raise AuthError("ไม่พบผู้ใช้")
        merged = {**asdict(current), **{k: v for k, v in changes.items() if v is not None or k == "staff_id"}}
        _validate(merged["role"], merged["display_name"], merged["staff_id"])
        with self._connect() as conn:
            row = conn.execute(
                f"""UPDATE app_users SET display_name = %s, role = %s, staff_id = %s, is_active = %s,
                        session_version = session_version + 1
                    WHERE user_id = %s RETURNING {self._cols()}""",
                (merged["display_name"], merged["role"],
                 uuid.UUID(merged["staff_id"]) if merged["staff_id"] else None, merged["is_active"],
                 uuid.UUID(user_id)),
            ).fetchone()
        return self._user(row)

    def set_password(self, user_id: str, password: str, *, temporary: bool) -> None:
        user = self.get(user_id)
        if user is None:
            raise AuthError("ไม่พบผู้ใช้")
        check_new_password(password, user.username)
        with self._connect() as conn:
            conn.execute(
                """UPDATE app_users SET password_hash = %s, must_change_password = %s,
                       session_version = session_version + 1 WHERE user_id = %s""",
                (hash_password(password), temporary, uuid.UUID(user_id)),
            )

    def touch_login(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE app_users SET last_login_at = NOW() WHERE user_id = %s", (uuid.UUID(user_id),))

    def end_sessions(self, user_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE app_users SET session_version = session_version + 1 WHERE user_id = %s",
                         (uuid.UUID(user_id),))


class Accounts:
    """Login, session lookup and account management on top of a user store."""

    def __init__(self, store, signer: Signer | None = None) -> None:
        self.store = store
        self.signer = signer or Signer.from_env()
        # Own keys for the step between password and code and for setting up: such a token can never
        # pass as a session cookie.
        self.pending_signer = Signer(hmac.new(self.signer.secret, b"orchestra-2fa-pending", hashlib.sha256).digest())
        self.setup_signer = Signer(hmac.new(self.signer.secret, b"orchestra-2fa-setup", hashlib.sha256).digest())
        self.limiter = LoginLimiter()
        self.code_limiter = LoginLimiter(limit=5, window_s=600, per_user=10)  # wrong 6-digit codes
        self._ended: dict[str, float] = {}  # owner-admin cookies signed out before they expire (mac -> exp)
        self._env_last_step: int | None = None  # the owner account's last code (no database row)
        self._lock = threading.Lock()

    def login(self, username: str, password: str, address: str) -> tuple[User, str]:
        username = (username or "").strip().lower()
        key = (username, address)
        if self.limiter.blocked(key):
            raise TooManyAttempts("ล็อกอินผิดหลายครั้ง กรุณารอ 10 นาทีแล้วลองใหม่")
        admin = env_admin()
        user = None
        # Exactly one scrypt on every path, so the time taken does not tell which user names exist.
        if admin and username == admin.username:
            verify_password(password, DUMMY_HASH)
            if hmac.compare_digest(password.encode(), os.environ["APP_PASSWORD"].encode()):
                user = admin
        else:
            found = self.store.by_username(username)
            ok = verify_password(password, found.password_hash if found else DUMMY_HASH)
            if found and found.is_active and ok:
                user = found
        if user is None:
            self.limiter.fail(key)
            raise AuthError("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
        self.limiter.clear(key)
        if user.totp_enabled:  # the password alone is not enough: the code comes next
            raise CodeNeeded(user, self.pending_signer.sign({
                "uid": user.user_id, "sv": user.session_version, "exp": int(time.time()) + PENDING_S,
                "n": secrets.token_hex(8)}))
        return user, self._finish(user)

    def _finish(self, user: User) -> str:
        if user.user_id != ENV_ADMIN_ID:
            self.store.touch_login(user.user_id)
        return self.token(user)

    # ------------------------------------------------------------ two-step sign-in

    def verify_code(self, pending: str | None, code: str, address: str) -> tuple[User, str, dict]:
        """Second step of signing in: the code from the app, or a recovery code."""
        payload = self.pending_signer.verify(pending) if pending else None
        if payload is None:
            raise AuthError("หมดเวลายืนยันตัวตน กรุณาเข้าสู่ระบบใหม่")
        key = (payload["uid"], address)
        if self.code_limiter.blocked(key):
            raise TooManyAttempts("กรอกรหัสผิดหลายครั้ง กรุณารอ 10 นาทีแล้วเข้าสู่ระบบใหม่")
        user = env_admin() if payload["uid"] == ENV_ADMIN_ID else self.store.get(payload["uid"])
        if user is None or not user.is_active or user.session_version != payload.get("sv") or not user.totp_enabled:
            raise AuthError("กรุณาเข้าสู่ระบบใหม่")
        used = self._check_code(user, code)
        if used is None:
            self.code_limiter.fail(key)
            raise AuthError("รหัสไม่ถูกต้อง (หรือใช้ไปแล้ว) · ใช้รหัสล่าสุดจากแอป หรือรหัสสำรอง")
        self.code_limiter.clear(key)
        return user, self._finish(user), used

    def _check_code(self, user: User, code: str | None) -> dict | None:
        """``{"used": "app"}``, ``{"used": "recovery", "recovery_left": n}`` or None. Each code works once."""
        code = tf.clean(code)
        if user.user_id == ENV_ADMIN_ID:  # secret from the environment, no recovery codes (Render is the way back)
            secret = env_totp_secret()
            with self._lock:
                step = tf.match(secret, code, last_step=self._env_last_step) if secret else None
                if step is not None:
                    self._env_last_step = step
            return {"used": "app"} if step is not None else None
        state = self.store.totp_state(user.user_id)
        if not state:
            return None
        if tf.is_app_code(code):
            step = tf.match(state["secret"], code, last_step=state["last_step"])
            return {"used": "app"} if step is not None and self.store.use_step(user.user_id, step) else None
        left = self.store.use_recovery(user.user_id, tf.hash_recovery(code)) if len(code) >= 8 else None
        return {"used": "recovery", "recovery_left": left} if left is not None else None

    def _require_code(self, user: User, code: str | None, address: str = "-") -> dict:
        key = (user.user_id, "settings:" + address)
        if self.code_limiter.blocked(key):
            raise TooManyAttempts("กรอกรหัสผิดหลายครั้ง กรุณารอ 10 นาที")
        used = self._check_code(user, code)
        if used is None:
            self.code_limiter.fail(key)
            raise AuthError("รหัสยืนยันตัวตนไม่ถูกต้อง (หรือใช้ไปแล้ว)")
        self.code_limiter.clear(key)
        return used

    def start_setup(self, user: User) -> dict:
        """A new secret, as a QR code for the app; nothing is saved until a code from it is typed."""
        if user.user_id != ENV_ADMIN_ID and not self.store.two_factor_ready():
            raise AuthError(NEED_2FA_SQL)
        secret = tf.new_secret()
        link = tf.uri(secret, user.username)
        token = self.setup_signer.sign({"uid": user.user_id, "s": secret, "exp": int(time.time()) + SETUP_S,
                                        "n": secrets.token_hex(8)})
        return {"secret": secret, "uri": link, "qr": tf.qr_svg(link), "setup_token": token,
                "owner_account": user.user_id == ENV_ADMIN_ID, "replacing": user.totp_enabled}

    def enable(self, user: User, setup_token: str, code: str, current_code: str | None = None,
               address: str = "-") -> dict:
        """Switch on (or move to a new phone): the first code from the app proves it was set up right."""
        payload = self.setup_signer.verify(setup_token) if setup_token else None
        if payload is None or payload.get("uid") != user.user_id:
            raise AuthError("หมดเวลาตั้งค่า กรุณาเริ่มใหม่ (สแกน QR code ใหม่)")
        if user.totp_enabled and user.user_id != ENV_ADMIN_ID:
            self._require_code(user, current_code, address)  # moving phones: the old one (or a recovery code) first
        key = (user.user_id, "setup:" + address)
        if self.code_limiter.blocked(key):
            raise TooManyAttempts("กรอกรหัสผิดหลายครั้ง กรุณารอ 10 นาที")
        step = tf.match(payload["s"], code)
        if step is None:
            self.code_limiter.fail(key)
            raise AuthError("รหัสไม่ถูกต้อง · ตรวจว่าเวลาในมือถือตั้งเป็นอัตโนมัติ แล้วกรอกรหัสที่แสดงอยู่ตอนนี้")
        self.code_limiter.clear(key)
        if user.user_id == ENV_ADMIN_ID:  # no row to save in: the owner puts it in Render (APP_TOTP_SECRET)
            return {"owner_account": True, "secret": payload["s"]}
        codes = tf.new_recovery_codes()
        self.store.enable_totp(user.user_id, payload["s"], step, [tf.hash_recovery(c) for c in codes])
        return {"recovery_codes": codes, "token": self.token(self.store.get(user.user_id))}

    def disable(self, user: User, code: str, address: str = "-") -> str:
        if user.user_id == ENV_ADMIN_ID:
            raise AuthError("บัญชีหลัก: ลบค่า APP_TOTP_SECRET ใน Render เพื่อปิด")
        if user.two_factor_required:
            raise AuthError("บทบาทนี้ต้องใช้การยืนยันตัวตน 2 ชั้นเสมอ · ถ้าเปลี่ยนมือถือ ให้ตั้งค่าใหม่แทนการปิด")
        if not user.totp_enabled:
            raise AuthError("ยังไม่ได้เปิดการยืนยันตัวตน 2 ชั้น")
        self._require_code(user, code, address)
        self.store.disable_totp(user.user_id)
        return self.token(self.store.get(user.user_id))

    def new_recovery_codes(self, user: User, code: str, address: str = "-") -> list[str]:
        if user.user_id == ENV_ADMIN_ID or not user.totp_enabled:
            raise AuthError("ใช้ได้เมื่อเปิดการยืนยันตัวตน 2 ชั้นของบัญชีนี้แล้ว")
        self._require_code(user, code, address)
        codes = tf.new_recovery_codes()
        self.store.set_recovery(user.user_id, [tf.hash_recovery(c) for c in codes])
        return codes

    def reset_two_factor(self, target_id: str, actor: User) -> User:
        """Admin: someone lost their phone and their recovery codes. They set it up again at next sign-in."""
        if target_id == actor.user_id:
            raise AuthError("บัญชีของตัวเอง: ตั้งค่าใหม่ที่หน้าความปลอดภัยของบัญชี")
        target = self.store.get(target_id)
        if target is None:
            raise AuthError("ไม่พบผู้ใช้")
        if not target.totp_enabled:
            raise AuthError("ผู้ใช้นี้ยังไม่ได้เปิดการยืนยันตัวตน 2 ชั้น")
        self.store.disable_totp(target_id)  # also signs them out everywhere
        return self.store.get(target_id)

    def recovery_left(self, user: User) -> int | None:
        if user.user_id == ENV_ADMIN_ID or not user.totp_enabled:
            return None
        state = self.store.totp_state(user.user_id)
        return len(state["recovery"]) if state else None

    def token(self, user: User) -> str:
        return self.signer.sign({"uid": user.user_id, "sv": user.session_version,
                                 "exp": int(time.time()) + SESSION_HOURS * 3600,
                                 "n": secrets.token_hex(8)})  # every sign-in is its own cookie (logout ends one)

    def logout(self, token: str | None) -> None:
        """End the session on the server too, not only in this browser (shared ward computers)."""
        payload = self.signer.verify(token) if token else None
        if payload is None:
            return
        if payload["uid"] == ENV_ADMIN_ID:  # no row to bump: remember the cookie until it expires
            with self._lock:
                now = time.time()
                self._ended = {m: exp for m, exp in self._ended.items() if exp > now}
                self._ended[token.rsplit(".", 1)[1]] = payload.get("exp", now)
        else:
            try:
                self.store.end_sessions(payload["uid"])  # signs this person out on every device
            except (AuthError, ValueError):
                pass

    def user_from_token(self, token: str | None) -> User | None:
        payload = self.signer.verify(token) if token else None
        if payload is None:
            return None
        if payload["uid"] == ENV_ADMIN_ID and token.rsplit(".", 1)[1] in self._ended:
            return None
        if payload["uid"] == ENV_ADMIN_ID:
            user = env_admin()
        else:
            try:
                user = self.store.get(payload["uid"])
            except ServiceUnavailable:
                return None  # database down: treat as signed out; login explains
        if user is None or not user.is_active or user.session_version != payload.get("sv"):
            return None
        return user

    def change_own_password(self, user: User, current: str, new: str) -> str:
        if user.user_id == ENV_ADMIN_ID:
            raise AuthError("รหัสผ่านของบัญชีหลักเปลี่ยนได้ที่ Render (APP_PASSWORD)")
        if not verify_password(current, user.password_hash):
            raise AuthError("รหัสผ่านปัจจุบันไม่ถูกต้อง")
        if current == new:
            raise AuthError("รหัสผ่านใหม่ต้องไม่ซ้ำรหัสเดิม")
        self.store.set_password(user.user_id, new, temporary=False)
        return self.token(self.store.get(user.user_id))
