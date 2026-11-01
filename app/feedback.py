"""Problem reports and suggestions from users, and errors the system ran into, in one place for the admin.

Users send a report from any page (the page and time are attached; no patient data is). Errors are
caught on the server (unexpected exceptions in requests, failures logged by background jobs) and in
the browser, grouped by kind, and counted. Free text is scrubbed of HN, ID / phone numbers and names
after a title before it is stored, because people may type them anyway.

Supabase mode keeps them in ``app_feedback`` / ``app_errors`` (migration 016); before that file is run,
and in demo mode, they are kept in memory (lost when the server restarts).
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from orchestra_engine.intake import mask_identifiers

from .store import StoreError

log = logging.getLogger("orchestra.feedback")

KINDS = {"PROBLEM": "แจ้งปัญหา", "IDEA": "ข้อเสนอแนะ", "QUESTION": "สอบถาม"}
STATUSES = {"NEW": "ใหม่", "SEEN": "รับทราบแล้ว", "DONE": "แก้แล้ว / ปิดเรื่อง"}
ERROR_STATUSES = {"NEW": "ใหม่", "DONE": "แก้แล้ว"}
SOURCES = {"server": "เซิร์ฟเวอร์", "browser": "เบราว์เซอร์", "job": "งานเบื้องหลัง"}
MAX_MESSAGE = 2000
MAX_NOTE = 500
PER_HOUR = 20           # reports one person may send in an hour
ERRORS_PER_HOUR = 30    # browser errors one person may send in an hour
NOTIFY_EVERY_S = 900    # at most one "new error" notification per 15 minutes
MEMORY_LIMIT = 500
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DIGITS = re.compile(r"\d+")
HEX_ID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def scrub(text, limit: int) -> str:
    """Free text without patient identifiers (best effort) or control characters, at most ``limit`` long."""
    text = CONTROL.sub(" ", str(text or "")).strip()
    return mask_identifiers(text[: limit * 2])[0][:limit].strip()


def clean_page(url) -> str | None:
    """Which page: the path, the planning day and the tab (nothing else from the address)."""
    if not url:
        return None
    parts = urlsplit(str(url)[:500])
    path = re.sub(r"[^A-Za-z0-9/_.-]", "", parts.path)[:80] or "/"
    day = (parse_qs(parts.query).get("date") or [""])[0]
    tab = parts.fragment if re.fullmatch(r"[A-Za-z0-9_-]{1,30}", parts.fragment or "") else ""
    return path + (f"?date={day}" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else "") + (f"#{tab}" if tab else "")


def fingerprint(source: str, where: str, message: str) -> str:
    """The same error from the same place counts as one (numbers and ids ignored)."""
    shape = DIGITS.sub("#", HEX_ID.sub("<id>", f"{source}|{where}|{message}"))
    return hashlib.sha256(shape.encode()).hexdigest()[:32]


def _who(user) -> dict:
    return {"user_id": user.user_id, "username": user.username, "display_name": user.display_name, "role": user.role}


def _table(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{name}",)).fetchone()["ok"]


class Feedback:
    def __init__(self, store, notify=None) -> None:
        self.store = store
        self.notify = notify  # Notifier.handle: tells admins (without the text people typed)
        self._lock = threading.RLock()
        self._memory: list[dict] = []
        self._errors: dict[str, dict] = {}
        self._next = 0
        self._sent: dict[tuple, deque] = defaultdict(deque)
        self._last_notice = 0.0
        self._busy = threading.local()
        self._db_down_until = 0.0  # after a failed write, keep to memory for a minute (no slow retries)

    @property
    def supabase(self) -> bool:
        return getattr(self.store, "mode", "demo") == "supabase"

    def _conn(self):
        return self.store.db.connect()

    def _db_ok(self) -> bool:
        return self.supabase and time.monotonic() >= self._db_down_until

    def _db_failed(self) -> None:
        self._db_down_until = time.monotonic() + 60

    def ready(self) -> bool:
        """True when reports are kept in the database (or in demo mode, where memory is all there is)."""
        if not self.supabase:
            return True
        try:
            with self._conn() as conn:
                return _table(conn, "app_feedback") and _table(conn, "app_errors")
        except Exception:
            return False

    def _allow(self, key: tuple, per_hour: int) -> bool:
        now = time.monotonic()
        with self._lock:
            sent = self._sent[key]
            while sent and now - sent[0] > 3600:
                sent.popleft()
            if len(sent) >= per_hour:
                return False
            sent.append(now)
            if len(self._sent) > 5000:
                self._sent.clear()
            return True

    def _id(self) -> int:
        """Ids of items kept in memory are negative (never the same as a database id)."""
        with self._lock:
            self._next += 1
            return -self._next

    # ------------------------------------------------------------- reports from people

    def submit(self, user, kind: str, message: str, page: str | None = None, agent: str | None = None) -> dict:
        if kind not in KINDS:
            raise StoreError("ประเภทไม่ถูกต้อง")
        text = scrub(message, MAX_MESSAGE)
        if len(text) < 3:
            raise StoreError("กรุณาเขียนรายละเอียดสั้น ๆ ว่าเกิดอะไรขึ้น หรืออยากให้ปรับอะไร")
        if not self._allow(("report", user.user_id), PER_HOUR):
            raise StoreError("ส่งเรื่องหลายครั้งเกินไปในชั่วโมงนี้ กรุณาลองใหม่ภายหลัง หรือติดต่อผู้ดูแลระบบโดยตรง")
        row = {"kind": kind, "message": text, "page": clean_page(page), "agent": scrub(agent, 200) or None,
               "status": "NEW", "created_at": datetime.now().astimezone(), **_who(user)}
        stored = False
        if self._db_ok():
            try:
                with self._conn() as conn:
                    if _table(conn, "app_feedback"):
                        row["id"] = conn.execute(
                            """INSERT INTO app_feedback (kind, message, page, user_agent, user_id, username, display_name, role)
                               VALUES (%(kind)s, %(message)s, %(page)s, %(agent)s, %(user_id)s, %(username)s,
                                       %(display_name)s, %(role)s) RETURNING feedback_id""", row).fetchone()["feedback_id"]
                        stored = True
            except Exception:
                self._db_failed()
                log.warning("feedback could not be saved in the database; kept in memory", exc_info=True)
        if not stored:
            row["id"] = self._id()
            with self._lock:
                self._memory.append(row)
                del self._memory[:-MEMORY_LIMIT]
        self._tell({"type": "feedback", "kind": kind, "role": user.role, "page": row["page"], "actor_id": user.user_id})
        return {"id": row["id"], "kept": "database" if stored else "memory"}

    def mine(self, user, limit: int = 10) -> list[dict]:
        """What this person has sent lately, with what the admin did about it."""
        rows = [r for r in self._memory if r["user_id"] == user.user_id]
        if self.supabase:
            try:
                with self._conn() as conn:
                    if _table(conn, "app_feedback"):
                        rows += conn.execute(
                            """SELECT feedback_id AS id, kind, message, page, status, admin_note, created_at, updated_at
                               FROM app_feedback WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""",
                            (user.user_id, limit)).fetchall()
            except Exception:
                log.warning("own feedback could not be read", exc_info=True)
        rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)[:limit]
        return [self._public(r) for r in rows]

    # ------------------------------------------------------------- errors

    def record_error(self, source: str, where: str, message: str, *, user=None, page: str | None = None) -> bool:
        """Count one error (never raises). False when it was not recorded (e.g. too many from one person)."""
        if getattr(self._busy, "on", False):  # an error while recording an error
            return False
        self._busy.on = True
        try:
            if source == "browser" and user is not None and not self._allow(("error", user.user_id), ERRORS_PER_HOUR):
                return False
            where, message = scrub(where, 200) or "-", scrub(message, 500) or "-"
            key = fingerprint(source, where, message)
            row = {"fingerprint": key, "source": source if source in SOURCES else "server", "where_at": where,
                   "message": message, "page": clean_page(page), "last_user": user.username if user else None}
            first = self._save_error(row)
            if first:
                self._tell({"type": "system_error", "source": row["source"], "where": where})
            return True
        except Exception:
            log.warning("error could not be recorded", exc_info=True)
            return False
        finally:
            self._busy.on = False

    def _save_error(self, row: dict) -> bool:
        """Insert or count; True when this error is new (or came back after it was marked fixed)."""
        if self._db_ok():
            try:
                with self._conn() as conn:
                    if _table(conn, "app_errors"):
                        old = conn.execute("SELECT status FROM app_errors WHERE fingerprint = %s FOR UPDATE",
                                           (row["fingerprint"],)).fetchone()
                        if old is None:
                            conn.execute(
                                """INSERT INTO app_errors (fingerprint, source, where_at, message, page, last_user)
                                   VALUES (%(fingerprint)s, %(source)s, %(where_at)s, %(message)s, %(page)s, %(last_user)s)
                                   ON CONFLICT (fingerprint) DO UPDATE SET occurrences = app_errors.occurrences + 1,
                                       last_seen = NOW()""", row)
                            return True
                        conn.execute(
                            """UPDATE app_errors SET occurrences = occurrences + 1, last_seen = NOW(), status = 'NEW',
                                   page = COALESCE(%(page)s, page), last_user = COALESCE(%(last_user)s, last_user),
                                   reopened = reopened OR status = 'DONE'
                               WHERE fingerprint = %(fingerprint)s""", row)
                        return old["status"] == "DONE"
            except Exception:
                self._db_failed()
                log.warning("error could not be saved in the database; kept in memory", exc_info=True)
        now = datetime.now().astimezone()
        with self._lock:
            old = self._errors.get(row["fingerprint"])
            if old:
                came_back = old["status"] == "DONE"
                old.update(occurrences=old["occurrences"] + 1, last_seen=now, status="NEW", reopened=old["reopened"] or came_back,
                           page=row["page"] or old["page"], last_user=row["last_user"] or old["last_user"])
                return came_back
            if len(self._errors) >= MEMORY_LIMIT:
                del self._errors[min(self._errors, key=lambda k: self._errors[k]["last_seen"])]
            self._errors[row["fingerprint"]] = {**row, "id": self._id(), "occurrences": 1, "first_seen": now,
                                                "last_seen": now, "status": "NEW", "admin_note": None, "reopened": False}
            return True

    def _tell(self, event: dict) -> None:
        if self.notify is None:
            return
        if event["type"] == "system_error":
            now = time.monotonic()
            with self._lock:
                if now - self._last_notice < NOTIFY_EVERY_S and self._last_notice:
                    return
                self._last_notice = now
        try:
            self.notify(event)
        except Exception:
            log.warning("feedback notification failed", exc_info=True)

    # ------------------------------------------------------------- admin

    @staticmethod
    def _public(r: dict) -> dict:
        out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(r).items()
               if k not in ("user_id", "agent", "user_agent")}
        if "kind" in out:
            out["kind_label"] = KINDS.get(out["kind"], out["kind"])
        out["status_label"] = STATUSES.get(out.get("status"), out.get("status"))
        return out

    def overview(self, *, show_done: bool = False, limit: int = 100) -> dict:
        reports = list(self._memory)
        errors = list(self._errors.values())
        ready = self.ready()
        if self.supabase and ready:
            try:
                with self._conn() as conn:
                    done = "" if show_done else "WHERE status <> 'DONE'"
                    reports += conn.execute(
                        f"""SELECT feedback_id AS id, kind, message, page, user_agent, username, display_name, role, status,
                                   admin_note, handled_by, created_at, updated_at
                            FROM app_feedback {done} ORDER BY created_at DESC LIMIT %s""", (limit,)).fetchall()
                    errors += conn.execute(
                        f"""SELECT error_id AS id, source, where_at, message, page, last_user, occurrences, first_seen,
                                   last_seen, status, admin_note, reopened
                            FROM app_errors {done} ORDER BY last_seen DESC LIMIT %s""", (limit,)).fetchall()
            except Exception:
                log.warning("feedback overview failed", exc_info=True)
        if not show_done:
            reports = [r for r in reports if r["status"] != "DONE"]
            errors = [e for e in errors if e["status"] != "DONE"]
        reports = sorted(reports, key=lambda r: r["created_at"], reverse=True)[:limit]
        errors = sorted(errors, key=lambda e: e["last_seen"], reverse=True)[:limit]
        return {
            "ready": ready, "kept_in_memory": not ready or not self.supabase,
            "reports": [{**self._public(r), "agent": r.get("user_agent") or r.get("agent")} for r in reports],
            "errors": [{**self._public(e), "source_label": SOURCES.get(e["source"], e["source"])} for e in errors],
            "new_reports": sum(1 for r in reports if r["status"] == "NEW"),
            "new_errors": sum(1 for e in errors if e["status"] == "NEW"),
            "kinds": KINDS, "statuses": STATUSES,
        }

    def update(self, what: str, item_id: int, status: str, note: str | None, actor: dict | None = None) -> dict:
        allowed = STATUSES if what == "report" else ERROR_STATUSES
        if status not in allowed:
            raise StoreError("สถานะไม่ถูกต้อง")
        note = scrub(note, MAX_NOTE) or None
        by = (actor or {}).get("name")
        with self._lock:
            pool = self._memory if what == "report" else list(self._errors.values())
            for r in pool:
                if r["id"] == item_id:
                    r.update(status=status, admin_note=note if note is not None else r.get("admin_note"),
                             updated_at=datetime.now().astimezone(), handled_by=by)
                    return {"id": item_id, "status": status}
        if self.supabase:
            with self._conn() as conn:
                table, key = ("app_feedback", "feedback_id") if what == "report" else ("app_errors", "error_id")
                if _table(conn, table):
                    extra = ", handled_by = %(by)s, updated_at = NOW()" if what == "report" else ", reopened = false"
                    cur = conn.execute(
                        f"""UPDATE {table} SET status = %(status)s, admin_note = COALESCE(%(note)s, admin_note){extra}
                            WHERE {key} = %(id)s""",  # noqa: S608 (fixed names)
                        {"status": status, "note": note, "by": by, "id": item_id})
                    if cur.rowcount:
                        return {"id": item_id, "status": status}
        raise StoreError("ไม่พบเรื่องนี้ (อาจถูกลบไปแล้ว)")

    def counts(self, conn=None) -> dict:
        """New reports and errors, for the status checklist."""
        reports = sum(1 for r in self._memory if r["status"] == "NEW")
        errors = sum(1 for e in self._errors.values() if e["status"] == "NEW")
        if conn is not None and _table(conn, "app_feedback") and _table(conn, "app_errors"):
            row = conn.execute("""SELECT (SELECT count(*) FROM app_feedback WHERE status = 'NEW') AS reports,
                                         (SELECT count(*) FROM app_errors WHERE status = 'NEW') AS errors""").fetchone()
            reports, errors = reports + row["reports"], errors + row["errors"]
        return {"reports": reports, "errors": errors}


class ErrorLogHandler(logging.Handler):
    """Errors that the app logs (background jobs, notifications, ...) also go to the admin's error list."""

    def __init__(self, feedback: Feedback) -> None:
        super().__init__(level=logging.ERROR)
        self.feedback = feedback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                message += f": {type(exc).__name__}: {exc}"
            self.feedback.record_error("job", record.name.replace("orchestra.", ""), message)
        except Exception:  # logging must never fail the caller
            pass
