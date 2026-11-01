"""User accounts, roles and sessions.

- Passwords: scrypt (salted), never stored in plain text.
- Sessions: HMAC-signed cookie {user id, session version, expiry}; bumping
  the user's session_version (password change, disable) signs them out.
- The account from APP_USERNAME / APP_PASSWORD always works as the owner's
  admin account, so the first real accounts can be created from the app.
- Accounts live in ``app_users`` (migration 003) in Supabase mode, or in
  memory in demo mode.
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
            return cls(hashlib.sha256(b"orchestra-session|" + material.encode()).digest())
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
    """At most 5 failed logins per user name + address in 10 minutes."""

    def __init__(self, limit: int = 5, window_s: int = 600) -> None:
        self.limit, self.window_s = limit, window_s
        self._fails: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: tuple[str, str]) -> list[float]:
        now = time.time()
        fails = [t for t in self._fails.get(key, []) if now - t < self.window_s]
        self._fails[key] = fails
        return fails

    def blocked(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return len(self._recent(key)) >= self.limit

    def fail(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._recent(key).append(time.time())

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

    def public(self) -> dict:
        data = asdict(self)
        data.pop("password_hash")
        data["role_label"] = ROLES.get(self.role, self.role)
        data["permissions"] = sorted(p for p in PERMISSIONS if can(self.role, p))
        return data


def env_admin() -> User | None:
    password = os.environ.get("APP_PASSWORD", "")
    if not password:
        return None
    version = int(hashlib.sha256(password.encode()).hexdigest()[:6], 16)  # new password -> old sessions end
    return User(ENV_ADMIN_ID, os.environ.get("APP_USERNAME", "orchestra").lower(),
                "ผู้ดูแลระบบหลัก (Render)", "admin", session_version=version)


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
        self._lock = threading.Lock()

    def ready(self) -> bool:
        return True

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

    def touch_login(self, user_id: str) -> None:
        if user_id in self._users:
            self._users[user_id].last_login_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class DbUserStore:
    """Supabase mode: accounts in ``app_users`` (migration 003)."""

    COLS = ("user_id, username, display_name, role, staff_id, is_active, must_change_password, "
            "session_version, password_hash, last_login_at")

    def __init__(self, database) -> None:
        self.db = database

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
        return User(
            str(row["user_id"]), row["username"], row["display_name"], row["role"],
            str(row["staff_id"]) if row["staff_id"] else None, row["is_active"], row["must_change_password"],
            row["session_version"], row["password_hash"],
            row["last_login_at"].isoformat() if row["last_login_at"] else None,
        )

    def ready(self) -> bool:
        return self._query("SELECT to_regclass('public.app_users') AS t")[0]["t"] is not None

    def get(self, user_id: str) -> User | None:
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            return None
        rows = self._query(f"SELECT {self.COLS} FROM app_users WHERE user_id = %s", (uid,), missing=[])
        return self._user(rows[0]) if rows else None

    def by_username(self, username: str) -> User | None:
        rows = self._query(f"SELECT {self.COLS} FROM app_users WHERE username = %s",
                           (username.strip().lower(),), missing=[])
        return self._user(rows[0]) if rows else None

    def list(self) -> list[User]:
        return [self._user(r) for r in self._query(f"SELECT {self.COLS} FROM app_users ORDER BY username")]

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
                        VALUES (%s, %s, %s, %s, %s) RETURNING {self.COLS}""",
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
                    WHERE user_id = %s RETURNING {self.COLS}""",
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


class Accounts:
    """Login, session lookup and account management on top of a user store."""

    def __init__(self, store, signer: Signer | None = None) -> None:
        self.store = store
        self.signer = signer or Signer.from_env()
        self.limiter = LoginLimiter()

    def login(self, username: str, password: str, address: str) -> tuple[User, str]:
        username = (username or "").strip().lower()
        key = (username, address)
        if self.limiter.blocked(key):
            raise TooManyAttempts("ล็อกอินผิดหลายครั้ง กรุณารอ 10 นาทีแล้วลองใหม่")
        admin = env_admin()
        user = None
        if admin and username == admin.username:
            if hmac.compare_digest(password.encode(), os.environ["APP_PASSWORD"].encode()):
                user = admin
        else:
            found = self.store.by_username(username)
            if found and found.is_active and verify_password(password, found.password_hash):
                user = found
            elif found is None:
                verify_password(password, hash_password("timing-equaliser"))
        if user is None:
            self.limiter.fail(key)
            raise AuthError("ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
        self.limiter.clear(key)
        if user.user_id != ENV_ADMIN_ID:
            self.store.touch_login(user.user_id)
        return user, self.token(user)

    def token(self, user: User) -> str:
        return self.signer.sign({"uid": user.user_id, "sv": user.session_version,
                                 "exp": int(time.time()) + SESSION_HOURS * 3600})

    def user_from_token(self, token: str | None) -> User | None:
        payload = self.signer.verify(token) if token else None
        if payload is None:
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
