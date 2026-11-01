"""Phone notifications: browser push (Web Push, free) and LINE (Messaging API).

Events come from the stores after a change is saved:

* ``schedule`` (booking, emergency, overrun, re-plan): each team member whose
  case is new, moved, re-timed or taken off the schedule gets one message
  listing their cases (kind ``case``); schedulers also get a summary of every
  emergency insert (kind ``emergency``).
* ``cssd``: CSSD reports a tray ready later than a booked case needs it
  (kind ``cssd``) -> schedulers and CSSD.
* ``readiness`` (an item of the pre-op checklist marked as a problem) and
  ``readiness_digest`` (the next day's cases that are not ready, once a day)
  (kind ``readiness``) -> schedulers and the case's surgeon.

Messages carry case codes, times and rooms only - never a patient's name or
HN - because they pass through Google/Apple push services and LINE. Delivery
runs in the background and never blocks or breaks the change itself; each
attempt is logged in ``notification_deliveries``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from orchestra_engine import fmt

from .auth import env_admin

log = logging.getLogger("orchestra.notify")

KINDS = {
    "case": "เคสของฉันถูกเพิ่ม ย้าย หรือเอาออกจากตาราง",
    "emergency": "มีเคสฉุกเฉินแทรก (สรุปสำหรับผู้จัดตาราง)",
    "cssd": "CSSD อบเครื่องมือไม่ทันเคสที่จองไว้",
    "readiness": "เคสยังไม่พร้อมผ่าตัด (มีปัญหา และสรุปเคสวันถัดไปวันละครั้ง)",
}
KIND_ROLES = {"case": None, "emergency": {"admin", "scheduler"}, "cssd": {"admin", "scheduler", "cssd"},
              "readiness": {"admin", "scheduler", "surgeon"}}
MAX_LINES = 6
CODE_MINUTES = 15


class NotifyUnavailable(Exception):
    """Notifications are not set up (e.g. the migration has not run); the message says what to do."""


def default_prefs(role: str) -> dict[str, bool]:
    return {kind: roles is None or role in roles for kind, roles in KIND_ROLES.items()}


@dataclass
class Target:
    user_id: str
    channel: str  # webpush / line
    address: str  # push endpoint / LINE user id
    keys: dict = field(default_factory=dict)
    label: str | None = None


@dataclass
class Message:
    user_id: str
    kind: str
    title: str
    lines: list[str]
    url: str = "/"

    @property
    def body(self) -> str:
        shown = self.lines[:MAX_LINES]
        more = len(self.lines) - len(shown)
        return "\n".join(shown + ([f"และอีก {more} รายการ"] if more > 0 else []))


# ------------------------------------------------------------------ storage

class MemoryNotifyStore:
    """Demo mode: everything in memory (lost on restart)."""

    def __init__(self) -> None:
        self._targets: dict[tuple[str, str], Target] = {}
        self._prefs: dict[str, dict] = {}
        self._codes: dict[str, tuple[str, datetime]] = {}
        self._log: list[dict] = []
        self._settings: dict[str, str] = {}

    def ready(self) -> bool:
        return True

    def targets(self, user_id: str) -> list[Target]:
        return [t for t in self._targets.values() if t.user_id == user_id]

    def add_target(self, target: Target) -> None:
        self._targets[(target.channel, target.address)] = target

    def remove_target(self, channel: str, address: str, user_id: str | None = None) -> bool:
        t = self._targets.get((channel, address))
        if t is None or (user_id is not None and t.user_id != user_id):
            return False
        del self._targets[(channel, address)]
        return True

    def prefs(self, user_id: str) -> dict | None:
        return self._prefs.get(user_id)

    def set_prefs(self, user_id: str, prefs: dict) -> None:
        self._prefs[user_id] = dict(prefs)

    def new_code(self, user_id: str) -> str:
        code = f"{secrets.randbelow(10**6):06d}"
        self._codes[code] = (user_id, datetime.now(timezone.utc) + timedelta(minutes=CODE_MINUTES))
        return code

    def take_code(self, code: str) -> str | None:
        user_id, expires = self._codes.pop(code, (None, None))
        return user_id if expires and expires > datetime.now(timezone.utc) else None

    def log(self, user_id: str, channel: str, kind: str, title: str, body: str, status: str, error: str | None) -> None:
        self._log.append({"user_id": user_id, "channel": channel, "kind": kind, "title": title, "body": body,
                          "status": status, "error": error, "created_at": datetime.now(timezone.utc).isoformat()})

    def recent(self, user_id: str, limit: int = 10) -> list[dict]:
        return [r for r in reversed(self._log) if r["user_id"] == user_id][:limit]

    def setting(self, key: str) -> str | None:
        return self._settings.get(key)

    def set_setting(self, key: str, value: str) -> None:
        self._settings[key] = value


class DbNotifyStore:
    """Supabase mode: tables from migration 008."""

    def __init__(self, database) -> None:
        self.db = database

    def _run(self, sql: str, params=(), *, rows: bool = False):
        from .db import DatabaseUnavailable

        try:
            with self.db.connect() as conn:
                if not conn.execute("SELECT to_regclass('public.notify_targets') IS NOT NULL AS ok").fetchone()["ok"]:
                    raise NotifyUnavailable("ยังไม่ได้รัน migration supabase/migrations/20261107000000_notifications.sql")
                cur = conn.execute(sql, params)
                return cur.fetchall() if rows else cur
        except DatabaseUnavailable as exc:
            raise NotifyUnavailable(f"เชื่อมต่อฐานข้อมูลไม่ได้: {exc}") from exc

    def ready(self) -> bool:
        try:
            self._run("SELECT 1")
            return True
        except NotifyUnavailable:
            return False

    def targets(self, user_id: str) -> list[Target]:
        return [Target(r["user_id"], r["channel"], r["address"], r["keys"] or {}, r["label"])
                for r in self._run("SELECT * FROM notify_targets WHERE user_id = %s", (user_id,), rows=True)]

    def add_target(self, t: Target) -> None:
        from psycopg.types.json import Jsonb

        self._run("""INSERT INTO notify_targets (channel, address, user_id, keys, label) VALUES (%s, %s, %s, %s, %s)
                     ON CONFLICT (channel, address) DO UPDATE SET user_id = EXCLUDED.user_id, keys = EXCLUDED.keys,
                         label = EXCLUDED.label""", (t.channel, t.address, t.user_id, Jsonb(t.keys), t.label))

    def remove_target(self, channel: str, address: str, user_id: str | None = None) -> bool:
        if user_id is None:
            return self._run("DELETE FROM notify_targets WHERE channel = %s AND address = %s", (channel, address)).rowcount > 0
        return self._run("DELETE FROM notify_targets WHERE channel = %s AND address = %s AND user_id = %s",
                         (channel, address, user_id)).rowcount > 0

    def prefs(self, user_id: str) -> dict | None:
        rows = self._run("SELECT prefs FROM notify_prefs WHERE user_id = %s", (user_id,), rows=True)
        return rows[0]["prefs"] if rows else None

    def set_prefs(self, user_id: str, prefs: dict) -> None:
        from psycopg.types.json import Jsonb

        self._run("""INSERT INTO notify_prefs (user_id, prefs) VALUES (%s, %s)
                     ON CONFLICT (user_id) DO UPDATE SET prefs = EXCLUDED.prefs""", (user_id, Jsonb(prefs)))

    def new_code(self, user_id: str) -> str:
        code = f"{secrets.randbelow(10**6):06d}"
        self._run("DELETE FROM line_link_codes WHERE user_id = %s OR expires_at < NOW()", (user_id,))
        self._run("INSERT INTO line_link_codes (code, user_id, expires_at) VALUES (%s, %s, NOW() + %s) "
                  "ON CONFLICT (code) DO UPDATE SET user_id = EXCLUDED.user_id, expires_at = EXCLUDED.expires_at",
                  (code, user_id, timedelta(minutes=CODE_MINUTES)))
        return code

    def take_code(self, code: str) -> str | None:
        rows = self._run("DELETE FROM line_link_codes WHERE code = %s RETURNING user_id, expires_at > NOW() AS valid",
                         (code,), rows=True)
        return rows[0]["user_id"] if rows and rows[0]["valid"] else None

    def log(self, user_id: str, channel: str, kind: str, title: str, body: str, status: str, error: str | None) -> None:
        self._run("""INSERT INTO notification_deliveries (user_id, channel, kind, title, body, status, error)
                     VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                  (user_id, channel, kind, title[:200], body, status, (error or "")[:300] or None))

    def recent(self, user_id: str, limit: int = 10) -> list[dict]:
        return [{**r, "created_at": r["created_at"].isoformat()} for r in self._run(
            """SELECT channel, kind, title, body, status, error, created_at FROM notification_deliveries
               WHERE user_id = %s ORDER BY created_at DESC LIMIT %s""", (user_id, limit), rows=True)]

    def setting(self, key: str) -> str | None:
        rows = self._run("SELECT value FROM app_settings WHERE key = %s", (key,), rows=True)
        return rows[0]["value"] if rows else None

    def set_setting(self, key: str, value: str) -> None:
        self._run("INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (key, value))


# ------------------------------------------------------------------ channels

class WebPushSender:
    """Browser push with a VAPID key pair made on first use and kept in app_settings."""

    def __init__(self, store, subject: str | None = None) -> None:
        self.store = store
        site = os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL")  # Render sets the latter
        self.subject = subject or os.environ.get("VAPID_SUBJECT") or site or "mailto:orchestra-notify@example.org"
        self._vapid = None
        self._public: str | None = None

    def public_key(self) -> str:
        self._load()
        return self._public

    def _load(self) -> None:
        if self._vapid is not None:
            return
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid

        pem = self.store.setting("vapid_private_pem")
        if pem is None:
            fresh = Vapid()
            fresh.generate_keys()
            self.store.set_setting("vapid_private_pem", fresh.private_pem().decode())
            pem = self.store.setting("vapid_private_pem")  # another worker may have won the race
        vapid = Vapid.from_pem(pem.encode())
        raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self._vapid, self._public = vapid, base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    def send(self, target: Target, payload: dict) -> tuple[str, str | None]:
        from pywebpush import WebPushException, webpush

        self._load()
        try:
            webpush({"endpoint": target.address, "keys": target.keys}, json.dumps(payload, ensure_ascii=False),
                    vapid_private_key=self._vapid, vapid_claims={"sub": self.subject}, ttl=12 * 3600, timeout=10)
            return "sent", None
        except WebPushException as exc:
            code = getattr(exc.response, "status_code", None)
            if code in (404, 410):
                return "removed", f"subscription expired ({code})"
            return "failed", f"push service {code}: {str(exc)[:200]}"
        except Exception as exc:  # network errors etc.: log, never raise into the app
            return "failed", str(exc)[:200]


class LineSender:
    """LINE Messaging API of the hospital's Official Account (env LINE_CHANNEL_*)."""

    API = "https://api.line.me/v2/bot/message"

    def __init__(self, token: str | None = None, secret: str | None = None, add_friend_url: str | None = None) -> None:
        self.token = token if token is not None else os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
        self.secret = secret if secret is not None else os.environ.get("LINE_CHANNEL_SECRET", "")
        self.add_friend_url = add_friend_url if add_friend_url is not None else os.environ.get("LINE_ADD_FRIEND_URL", "")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.secret)

    def verify(self, body: bytes, signature: str | None) -> bool:
        if not self.secret or not signature:
            return False
        digest = hmac.new(self.secret.encode(), body, hashlib.sha256).digest()
        return hmac.compare_digest(base64.b64encode(digest).decode(), signature)

    def _post(self, path: str, payload: dict) -> tuple[str, str | None]:
        request = urllib.request.Request(
            f"{self.API}/{path}", data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                return "sent", None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            return "failed", f"LINE {exc.code}: {detail}"
        except Exception as exc:
            return "failed", str(exc)[:200]

    def push(self, to: str, text: str) -> tuple[str, str | None]:
        return self._post("push", {"to": to, "messages": [{"type": "text", "text": text[:4900]}]})

    def reply(self, reply_token: str, text: str) -> tuple[str, str | None]:
        return self._post("reply", {"replyToken": reply_token, "messages": [{"type": "text", "text": text}]})


# ------------------------------------------------------------------ messages

def _when(a) -> str:
    return f"{fmt(a.start_min)}-{fmt(a.end_min)} ห้อง {a.or_id}"


def describe_change(change, staff_id: str, cases: dict, cause: str | None = None) -> str | None:
    """One line for one person about one changed case (no patient identifiers)."""
    b, a = change.before, change.after
    in_b = b is not None and staff_id in b.staff_ids
    in_a = a is not None and staff_id in a.staff_ids
    case = cases.get(change.case_id)
    tag = change.case_id + (" (ฉุกเฉิน)" if case is not None and case.urgency.value == "EMERGENCY" else "")
    if a is None:
        if not in_b:
            return None
        return f"{tag}: งดผ่าตัด ({_when(b)})" if cause == "cancel" else f"{tag}: ถูกเอาออกจากตาราง (รอจัดวันใหม่)"
    if b is None:
        return f"{tag}: เคสใหม่ {_when(a)}" if in_a else None
    if in_a and not in_b:
        return f"{tag}: คุณถูกจัดเข้าทีม {_when(a)}"
    if in_b and not in_a:
        return f"{tag}: คุณไม่อยู่ในทีมแล้ว (เดิม {_when(b)})"
    if not in_a:
        return None
    if (a.start_min, a.or_id) != (b.start_min, b.or_id):
        return f"{tag}: ย้ายเป็น {_when(a)} (เดิม {fmt(b.start_min)} ห้อง {b.or_id})"
    if a.end_min != b.end_min:
        return f"{tag}: เวลาเสร็จเปลี่ยนเป็น {fmt(a.end_min)} (เดิม {fmt(b.end_min)})"
    return None  # equipment / bed swaps do not concern the team


def build_messages(event: dict, users: list) -> list[Message]:
    """Messages for one event, before preferences are applied."""
    actor = event.get("actor_id")
    day = event.get("day")
    url = f"/?date={day}" if day else "/"
    out: list[Message] = []
    if event["type"] == "schedule":
        by_staff = {u.staff_id: u for u in users if u.staff_id}
        lines: dict[str, list[str]] = defaultdict(list)
        for change in event.get("changes", []):
            people = set(change.before.staff_ids if change.before else ()) | set(change.after.staff_ids if change.after else ())
            for sid in sorted(people):
                user = by_staff.get(sid)
                if user is None or user.user_id == actor:
                    continue
                line = describe_change(change, sid, event.get("cases", {}), event.get("cause"))
                if line:
                    lines[user.user_id].append(line)
        when = f" วันที่ {day}" if day else ""
        out += [Message(uid, "case", f"ตารางผ่าตัดของคุณเปลี่ยน{when}", ls, url) for uid, ls in lines.items()]
        if event.get("cause") == "emergency":
            new = event.get("new_case")
            placed = next((c.after for c in event.get("changes", []) if c.case_id == new and c.after), None)
            summary = [f"{new}: " + (f"เข้า {_when(placed)}" if placed else "ยังหาห้องหรือทีมไม่ได้ ต้องเปิดห้อง/เรียกทีมเพิ่ม")]
            moved = sum(1 for c in event.get("changes", []) if c.case_id != new and c.after is not None)
            removed = len(event.get("unscheduled", []))
            if moved:
                summary.append(f"ย้าย/เปลี่ยนทีม {moved} เคส")
            if removed:
                summary.append(f"รอจัดวันใหม่ {removed} เคส")
            if event.get("fatigue"):
                summary.append(f"บุคลากรเกินเกณฑ์ความเหนื่อยล้า {event['fatigue']} คน")
            out += [Message(u.user_id, "emergency", f"เคสฉุกเฉินแทรก{when}", summary, url)
                    for u in users if u.role in KIND_ROLES["emergency"] and u.user_id != actor]
    elif event["type"] == "cssd":
        hit = event.get("cases", [])
        body = [f"{event['unit']} พร้อมใช้ {event['ready']}" if event.get("ready") else f"{event['unit']}: {event.get('status')}",
                "กระทบเคส: " + ", ".join(hit) if hit else "ตรวจตารางวันนี้"]
        out += [Message(u.user_id, "cssd", f"CSSD: {event['unit']} ไม่ทันเคสที่จองไว้", body, url)
                for u in users if u.role in KIND_ROLES["cssd"] and u.user_id != actor]
    elif event["type"] == "readiness":  # one checklist item flagged (the note stays in the app)
        title = f"{event['case']} มีปัญหาความพร้อม: {event['item']}"
        body = [f"วันที่ {day} {event['when']}" if day else event["when"], "ดูรายละเอียดในระบบ"]
        out += [Message(u.user_id, "readiness", title, body, url + "#ready") for u in users
                if u.user_id != actor and (u.role in ("admin", "scheduler")
                                           or (u.role == "surgeon" and u.staff_id and u.staff_id == event.get("surgeon_id")))]
    elif event["type"] == "readiness_digest":  # the next day's cases that are not ready
        cases = event.get("cases", [])
        lines = [f"{c['case']} {c['when']}: {c['missing']}" for c in cases]
        out += [Message(u.user_id, "readiness", f"เคสวันที่ {day} ยังไม่พร้อม {len(cases)} เคส", lines, url + "#ready")
                for u in users if u.role in ("admin", "scheduler")]
        for u in users:
            own = [f"{c['case']} {c['when']}: {c['missing']}" for c in cases
                   if u.role == "surgeon" and u.staff_id and c.get("surgeon_id") == u.staff_id]
            if own:
                out.append(Message(u.user_id, "readiness", f"เคสของคุณวันที่ {day} ยังไม่พร้อม {len(own)} เคส", own,
                                   url + "#ready"))
    return out


# ------------------------------------------------------------------ notifier

class Notifier:
    def __init__(self, accounts, store, webpush: WebPushSender | None = None, line: LineSender | None = None,
                 *, background: bool = True) -> None:
        self.accounts = accounts
        self.store = store
        self.webpush = webpush or WebPushSender(store)
        self.line = line or LineSender()
        self.background = background
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="notify") if background else None
        self.base_url = (os.environ.get("APP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")

    # -- who
    def users(self) -> list:
        users = [u for u in self.accounts.store.list() if u.is_active]
        owner = env_admin()
        return users + ([owner] if owner else [])

    def prefs(self, user) -> dict[str, bool]:
        saved = self.store.prefs(user.user_id) or {}
        return {**default_prefs(user.role), **{k: bool(v) for k, v in saved.items() if k in KINDS}}

    # -- events from the stores
    def handle(self, event: dict) -> None:
        if self._pool is not None:
            self._pool.submit(self._handle, event)
        else:
            self._handle(event)

    def _handle(self, event: dict) -> None:
        try:
            if not self.store.ready():
                return
            users = {u.user_id: u for u in self.users()}
            for message in build_messages(event, list(users.values())):
                user = users.get(message.user_id)
                if user is not None and self.prefs(user).get(message.kind):
                    self.deliver(message)
        except Exception:  # notifications must never break scheduling
            log.exception("notification event failed")

    def deliver(self, message: Message) -> list[str]:
        statuses = []
        for target in self.store.targets(message.user_id):
            if target.channel == "webpush":
                status, error = self.webpush.send(target, {"title": message.title, "body": message.body,
                                                           "url": message.url, "tag": message.kind})
            elif target.channel == "line" and self.line.enabled:
                link = f"\n{self.base_url}{message.url}" if self.base_url else ""
                status, error = self.line.push(target.address, f"{message.title}\n{message.body}{link}")
            else:
                continue
            if status == "removed":
                self.store.remove_target(target.channel, target.address)
            self.store.log(message.user_id, target.channel, message.kind, message.title, message.body, status, error)
            statuses.append(status)
        return statuses

    # -- settings for the signed-in user
    def status(self, user) -> dict:
        if not self.store.ready():
            return {"ready": False, "kinds": KINDS}
        targets = self.store.targets(user.user_id)
        return {
            "ready": True, "kinds": KINDS, "prefs": self.prefs(user),
            "webpush": {"public_key": self.webpush.public_key(),
                        "devices": [t.label or "อุปกรณ์" for t in targets if t.channel == "webpush"],
                        "endpoints": [t.address for t in targets if t.channel == "webpush"]},
            "line": {"enabled": self.line.enabled, "linked": any(t.channel == "line" for t in targets),
                     "add_friend_url": self.line.add_friend_url or None},
            "recent": self.store.recent(user.user_id),
        }

    def subscribe(self, user, subscription: dict, label: str | None) -> None:
        endpoint = subscription.get("endpoint") or ""
        keys = subscription.get("keys") or {}
        if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("ข้อมูลการสมัครรับแจ้งเตือนไม่ถูกต้อง")
        self.store.add_target(Target(user.user_id, "webpush", endpoint,
                                     {"p256dh": keys["p256dh"], "auth": keys["auth"]}, (label or "")[:200] or None))

    def unsubscribe(self, user, endpoint: str) -> bool:
        return self.store.remove_target("webpush", endpoint, user.user_id)

    def set_prefs(self, user, prefs: dict) -> dict:
        self.store.set_prefs(user.user_id, {k: bool(v) for k, v in prefs.items() if k in KINDS})
        return self.prefs(user)

    def link_code(self, user) -> str:
        if not self.line.enabled:
            raise ValueError("ผู้ดูแลยังไม่ได้ตั้งค่า LINE Official Account")
        return self.store.new_code(user.user_id)

    def unlink_line(self, user) -> int:
        return sum(self.store.remove_target("line", t.address, user.user_id)
                   for t in self.store.targets(user.user_id) if t.channel == "line")

    def test(self, user) -> list[str]:
        statuses = self.deliver(Message(user.user_id, "test", "ทดสอบการแจ้งเตือน ORchestraAI",
                                        [f"ถึง {user.display_name}: ถ้าเห็นข้อความนี้ แปลว่าการแจ้งเตือนใช้งานได้"]))
        if not statuses:
            raise ValueError("ยังไม่ได้เปิดการแจ้งเตือนบนอุปกรณ์ใดหรือเชื่อม LINE")
        return statuses

    # -- LINE webhook: users send their one-time code to the Official Account
    def line_webhook(self, body: bytes, signature: str | None) -> int:
        if not self.line.verify(body, signature):
            raise PermissionError("bad signature")
        handled = 0
        for event in json.loads(body or b"{}").get("events", []):
            source = (event.get("source") or {}).get("userId")
            reply = event.get("replyToken")
            if event.get("type") == "unfollow" and source:
                self.store.remove_target("line", source)
            elif event.get("type") == "follow" and reply:
                self.line.reply(reply, "สวัสดีครับ พิมพ์รหัส 6 หลักจากหน้า 'แจ้งเตือน' ใน ORchestraAI เพื่อเชื่อมบัญชี")
            elif event.get("type") == "message" and (event.get("message") or {}).get("type") == "text" and source:
                text = "".join(ch for ch in event["message"].get("text", "") if ch.isdigit())
                user_id = self.store.take_code(text) if len(text) == 6 else None
                user = next((u for u in self.users() if u.user_id == user_id), None) if user_id else None
                if user is not None:
                    self.store.add_target(Target(user.user_id, "line", source, {}, "LINE"))
                    handled += 1
                    if reply:
                        self.line.reply(reply, f"เชื่อม LINE กับบัญชี ORchestraAI ของ {user.display_name} แล้ว "
                                               "คุณจะได้รับแจ้งเตือนตารางผ่าตัดทางนี้")
                elif reply:
                    self.line.reply(reply, "รหัสไม่ถูกต้องหรือหมดอายุ (15 นาที) กรุณาขอรหัสใหม่ในหน้า 'แจ้งเตือน' ของ ORchestraAI")
        return handled


__all__ = ["DbNotifyStore", "KINDS", "LineSender", "MemoryNotifyStore", "Message", "NotifyUnavailable", "Notifier",
           "Target", "WebPushSender", "build_messages", "default_prefs", "describe_change"]
