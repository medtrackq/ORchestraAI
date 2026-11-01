"""Connection to the hospital information system (HIS).

Hospital systems usually sit inside the hospital network, where a cloud server cannot reach them, so
the HIS calls ORchestraAI (outbound HTTPS from the hospital) with an API key:

* ``POST /api/his/v1/surgery-requests``: a surgery request (order) is created, changed or cancelled;
  it lands on the waiting list, keyed by the HIS order number (``his_ref``), so sending it again is safe.
* ``GET /api/his/v1/surgery-requests/{his_ref}`` and ``GET /api/his/v1/schedule``: what became of it.

Everything that arrives, from the API, the admin's test form or a future connector that pulls from a
hospital API, goes through one pipeline (``His.receive``): codes are matched (mapping table, staff code,
ICD-9-CM, name), unknown codes wait in the inbox until the admin matches them, and each message is kept
with its outcome. The message itself holds the HN and name: it is cleared once applied, and after
``INBOX_KEEP_DAYS`` when it never could be. Supabase mode only (migration 017).
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from orchestra_engine.intake import mask_identifiers

from .db import case_code
from .store import StoreError

log = logging.getLogger("orchestra.his")

SCOPES = {"requests:write": "ส่งใบขอผ่าตัด (สร้าง แก้ไข ยกเลิก)", "schedule:read": "อ่านตารางผ่าตัดและสถานะคำขอ"}
KINDS = {"surgeon": "ศัลยแพทย์", "procedure": "หัตถการ"}
STATUS_TH = {"APPLIED": "บันทึกแล้ว", "UNCHANGED": "ไม่มีอะไรเปลี่ยน", "NEEDS_MAPPING": "รอจับคู่รหัส",
             "NEEDS_ACTION": "ต้องดำเนินการในระบบ", "REJECTED": "ข้อมูลไม่ถูกต้อง"}
URGENCIES = ("ELECTIVE", "URGENT", "EMERGENCY")
KEY_PREFIX = "orc_"
PER_MINUTE = 300          # calls per key per minute
ITEMS_PER_MINUTE = 1200   # requests per key per minute (a batch counts each request in it)
ALERTS_PER_WINDOW = 20    # HIS notifications per 10 minutes (a misbehaving HIS must not flood phones);
EMERGENCIES_PER_WINDOW = 60  # emergency requests have their own, higher limit
ALERT_WINDOW_S = 600
DETAIL_MAX = 500          # his_inbox.detail
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
FAILS_PER_MINUTE = 30     # wrong keys from one address per minute (then 429 without a database lookup)
FAILS_ALL_PER_MINUTE = 600
READY_CHECK_S = 300       # migration 017 found: checked again after this
ANON_PREFIX = "anon:"     # his_ref of a case whose identity was removed (privacy.apply): a one-way hash
INBOX_KEEP_DAYS = 30      # a message that never applied keeps its HN / name this long
INBOX_DROP_DAYS = 180     # inbox rows (without patient data) are kept this long
BATCH_MAX = 200
NEED_SQL = "ยังไม่ได้เปิดการเชื่อมต่อ HIS (ต้องรัน SQL ไฟล์ 017 ก่อน)"


class HisUnavailable(StoreError):
    """Supabase or migration 017 missing."""


class HisAuthError(Exception):
    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Client:
    key_id: str | None
    name: str
    scopes: tuple[str, ...]

    def user(self):
        """Who is shown patient data, for the PDPA access log."""
        from .auth import User

        return User(f"his:{self.key_id}", self.name[:60], f"HIS: {self.name}", "his")


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _norm(text) -> str:
    return re.sub(r"[\s\-_.:/()]", "", str(text or "")).lower()


def _person(text) -> str:
    text = re.sub(r"\([^()]*\)", "", str(text or "")[:200])
    text = re.sub(r"^(ศ\.|รศ\.|ผศ\.|นพ\.|พญ\.|ทพ\.|ทญ\.|dr\.?|prof\.?)+", "", text.strip(), flags=re.I)
    return re.sub(r"[\s.]", "", text).lower()


def _day(value, field: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise StoreError(f"{field} ต้องเป็นวันที่แบบ YYYY-MM-DD") from exc


def normalize(payload: dict) -> dict:
    """Check one request from the HIS; raises ``StoreError`` with what is wrong."""
    if not isinstance(payload, dict):
        raise StoreError("ข้อมูลต้องเป็น JSON object")
    ref = str(payload.get("his_ref") or "").strip()
    if not ref or len(ref) > 100 or CONTROL.search(ref) or ref.startswith(ANON_PREFIX):
        raise StoreError("his_ref (เลขที่ใบขอผ่าตัดของ HIS) ต้องมี ยาวไม่เกิน 100 ตัวอักษร และไม่มีอักขระควบคุม")
    action = str(payload.get("action") or "upsert").lower()
    if action not in ("upsert", "cancel"):
        raise StoreError("action ต้องเป็น upsert หรือ cancel")
    msg = {"his_ref": ref, "action": action}
    if action == "cancel":
        return msg
    for field, label in (("hn", "hn"), ("patient_name", "patient_name"), ("surgeon_code", "surgeon_code")):
        value = str(payload.get(field) or "").strip()
        if not value:
            raise StoreError(f"ต้องมี {label}")
        msg[field] = value[:150]
    msg["hn"] = msg["hn"][:50]
    msg["surgeon_name"] = str(payload.get("surgeon_name") or "").strip()[:200] or None
    msg["procedure_code"] = str(payload.get("procedure_code") or "").strip()[:100] or None
    # Shown to the admin and in notifications: no HN / patient name even if the HIS put one in the label.
    msg["procedure_name"] = mask_identifiers(str(payload.get("procedure_name") or "").strip()[:200])[0] or None
    if not msg["procedure_code"] and not msg["procedure_name"]:
        raise StoreError("ต้องมี procedure_code หรือ procedure_name")
    urgency = str(payload.get("urgency") or "ELECTIVE").upper()
    if urgency not in URGENCIES:
        raise StoreError("urgency ต้องเป็น ELECTIVE, URGENT หรือ EMERGENCY")
    msg["urgency"] = urgency
    duration = payload.get("duration_mins")
    if duration not in (None, ""):
        try:
            duration = int(duration)
        except (TypeError, ValueError) as exc:
            raise StoreError("duration_mins ต้องเป็นตัวเลข (นาที)") from exc
        if not 15 <= duration <= 720:
            raise StoreError("duration_mins ต้องอยู่ระหว่าง 15-720 นาที")
    msg["duration_mins"] = duration or None
    msg["requested_date"] = _day(payload.get("requested_date"), "requested_date")
    msg["due_date"] = _day(payload.get("due_date"), "due_date")
    msg["requires_icu"] = bool(payload.get("requires_icu")) if payload.get("requires_icu") is not None else None
    msg["airborne"] = bool(payload.get("airborne"))
    return msg


def anon_ref(ref: str) -> str:
    """What ``privacy.apply`` leaves in ``his_ref``: the order number cannot be read back from it, but the
    same order sent again still finds the case (so it is neither recreated nor given its name back)."""
    return ANON_PREFIX + hashlib.sha256(ref.encode()).hexdigest()


ANON_REF_SQL = "'" + ANON_PREFIX + "' || encode(sha256(convert_to(his_ref, 'UTF8')), 'hex')"


def purge_inbox(conn) -> int:
    """Retention for the inbox (run on each message and by the daily PDPA job, so it also happens when the
    HIS stops sending): HN / names of messages that never applied go after ``INBOX_KEEP_DAYS``."""
    cleared = conn.execute("""UPDATE his_inbox SET payload = NULL WHERE payload IS NOT NULL
                              AND received_at < NOW() - make_interval(days => %s)""", (INBOX_KEEP_DAYS,)).rowcount
    conn.execute("DELETE FROM his_inbox WHERE received_at < NOW() - make_interval(days => %s)", (INBOX_DROP_DAYS,))
    return cleared


def _stored(msg: dict) -> dict:
    return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in msg.items()}


def ready(conn) -> bool:
    return conn.execute("""SELECT to_regclass('public.his_inbox') IS NOT NULL AND EXISTS (
                               SELECT 1 FROM information_schema.columns WHERE table_schema = 'public'
                               AND table_name = 'surgery_requests' AND column_name = 'his_ref') AS ok""").fetchone()["ok"]


class His:
    def __init__(self, store, notify=None, access=None) -> None:
        self.store = store
        self.notify = notify    # Notifier.handle (no HN or names in these events)
        self.access = access    # privacy.AccessLog: the HIS reading patient data is logged too
        self._lock = threading.Lock()
        self._calls: dict[str, deque] = defaultdict(deque)
        self._fails: dict[str, deque] = defaultdict(deque)
        self._items: dict[str, deque] = defaultdict(deque)
        self._alerts: dict[str, deque] = defaultdict(deque)
        self._ready_until = 0.0

    # ------------------------------------------------------------- plumbing

    def _db(self):
        if getattr(self.store, "mode", "demo") != "supabase":
            raise HisUnavailable("การเชื่อมต่อ HIS ใช้ได้เมื่อเชื่อมฐานข้อมูล Supabase แล้วเท่านั้น")
        return self.store

    def _tell(self, event: dict) -> None:
        if self.notify is None:
            return
        kind = "emergency" if event.get("what") == "emergency" else "other"
        limit = EMERGENCIES_PER_WINDOW if kind == "emergency" else ALERTS_PER_WINDOW
        now = time.monotonic()
        with self._lock:
            sent = self._alerts[kind]
            while sent and now - sent[0] > ALERT_WINDOW_S:
                sent.popleft()
            if len(sent) > limit:  # already said that it went quiet
                return
            sent.append(now)
            if len(sent) > limit:
                event = {"type": "his_alert", "what": "action" if kind == "emergency" else "mapping",
                         "detail": "HIS ส่งเรื่องที่ต้องดูเข้ามาจำนวนมาก หยุดแจ้งเตือนไว้ 10 นาที "
                                   "ดูทั้งหมดได้ที่รายการรอผ่าตัด และหน้าข้อมูลโรงพยาบาล แท็บเชื่อมต่อ HIS"}
        try:
            self.notify({**event, "detail": str(event.get("detail", ""))[:300]})
        except Exception:
            log.warning("HIS notification failed", exc_info=True)

    def _world(self, conn):
        """What the insert and the activity log need to know about the schema (no full day is loaded:
        a batch from the HIS may hold hundreds of requests)."""
        cols = {(r["table_name"], r["column_name"]) for r in conn.execute(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name IN ('surgery_requests', 'orchestra_activity')""")}
        return SimpleNamespace(day=self.store.db.today(), has=lambda t, c: (t, c) in cols,
                               has_activity=("orchestra_activity", "activity_id") in cols,
                               has_actor_col=("orchestra_activity", "actor") in cols)

    def _log(self, conn, title: str, actor: str | None, world=None) -> None:
        self.store.db.log_activity(conn, world or self._world(conn), {"kind": "his", "title": title, "log": [],
                                                                     "changes": [], "notifications": [], "actor": actor})

    # ------------------------------------------------------------- keys

    def create_key(self, name: str, scopes: list[str], actor: dict | None = None) -> dict:
        name = re.sub(r"\s+", " ", str(name or "")).strip()[:100]
        if not name:
            raise StoreError("ตั้งชื่อ key เช่น HOSxP โรงพยาบาล")
        scopes = sorted({s for s in scopes or [] if s in SCOPES})
        if not scopes:
            raise StoreError("เลือกสิทธิ์อย่างน้อย 1 อย่าง")
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        key_id = uuid.uuid4()
        with self._db()._transaction(write=True) as conn:
            if not ready(conn):
                raise HisUnavailable(NEED_SQL)
            conn.execute("""INSERT INTO his_api_keys (key_id, name, prefix, key_hash, scopes, created_by)
                            VALUES (%s, %s, %s, %s, %s, %s)""",
                         (key_id, name, key[:10], _hash(key), scopes, (actor or {}).get("name")))
            self._log(conn, f"สร้าง API key สำหรับ HIS: {name} ({', '.join(scopes)})", (actor or {}).get("name"))
        return {"id": str(key_id), "name": name, "key": key, "prefix": key[:10], "scopes": scopes}

    def revoke_key(self, key_id: str, actor: dict | None = None) -> dict:
        try:
            kid = uuid.UUID(key_id)
        except ValueError as exc:
            raise StoreError("ไม่พบ key นี้") from exc
        with self._db()._transaction(write=True) as conn:
            if not ready(conn):
                raise HisUnavailable(NEED_SQL)
            row = conn.execute("UPDATE his_api_keys SET revoked_at = NOW() WHERE key_id = %s AND revoked_at IS NULL "
                               "RETURNING name", (kid,)).fetchone()
            if row is None:
                raise StoreError("ไม่พบ key นี้ หรือยกเลิกไปแล้ว")
            self._log(conn, f"ยกเลิก API key ของ HIS: {row['name']}", (actor or {}).get("name"))
        return {"revoked": key_id}

    def _too_many_fails(self, address: str, now: float) -> bool:
        with self._lock:
            for key, limit in ((address, FAILS_PER_MINUTE), ("*", FAILS_ALL_PER_MINUTE)):
                fails = self._fails.get(key)
                while fails and now - fails[0] > 60:
                    fails.popleft()
                if fails is not None and not fails and key != "*":
                    del self._fails[key]
                elif fails is not None and len(fails) >= limit:
                    return True
        return False

    def _failed(self, address: str, now: float) -> None:
        with self._lock:
            if len(self._fails) > 10_000:  # many addresses: forget the quiet ones
                for key in [k for k, v in self._fails.items() if not v or now - v[-1] > 60]:
                    del self._fails[key]
            self._fails[address].append(now)
            self._fails["*"].append(now)

    def _ready(self, conn) -> bool:
        """``ready`` for the API calls, remembered for a while once true (every call would ask otherwise)."""
        if time.monotonic() < self._ready_until:
            return True
        ok = ready(conn)
        if ok:
            self._ready_until = time.monotonic() + READY_CHECK_S
        return ok

    def authenticate(self, raw: str | None, scope: str | None, address: str | None = None) -> Client:
        """The caller's key (``Authorization: Bearer <key>`` or ``X-API-Key``) with the scope asked for
        (``None``: any). Wrong keys are limited per address (and in all), so guessing cannot load the database."""
        now = time.monotonic()
        address = address or "unknown"
        if self._too_many_fails(address, now):
            raise HisAuthError("ส่ง API key ผิดหลายครั้งเกินไป ลองใหม่ในอีก 1 นาที", 429)
        raw = (raw or "").strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw.startswith(KEY_PREFIX) or len(raw) > 100:
            self._failed(address, now)
            raise HisAuthError("ต้องส่ง API key ใน header Authorization: Bearer <key>")
        store = self._db()
        with store._transaction(write=False) as conn:
            if not self._ready(conn):
                raise HisUnavailable(NEED_SQL)
            row = conn.execute("""SELECT key_id, name, scopes FROM his_api_keys
                                  WHERE key_hash = %s AND revoked_at IS NULL""", (_hash(raw),)).fetchone()
        if row is None:
            self._failed(address, now)
            raise HisAuthError("API key ไม่ถูกต้องหรือถูกยกเลิกแล้ว")
        if not row["scopes"] or (scope is not None and scope not in row["scopes"]):
            raise HisAuthError(f"key นี้ไม่มีสิทธิ์ {scope}" if scope else "key นี้ไม่มีสิทธิ์ใดเลย", 403)
        now = time.monotonic()
        with self._lock:
            calls = self._calls[str(row["key_id"])]
            while calls and now - calls[0] > 60:
                calls.popleft()
            if len(calls) >= PER_MINUTE:
                raise HisAuthError(f"เรียกเกิน {PER_MINUTE} ครั้งต่อนาที", 429)
            calls.append(now)
        with store.db.connect() as conn:
            conn.execute("""UPDATE his_api_keys SET last_used_at = NOW() WHERE key_id = %s
                            AND (last_used_at IS NULL OR last_used_at < NOW() - INTERVAL '1 minute')""", (row["key_id"],))
        return Client(str(row["key_id"]), row["name"], tuple(row["scopes"] or ()))

    def charge(self, client: Client, extra: int) -> None:
        """Count requests beyond the call itself (a batch) against ``ITEMS_PER_MINUTE``."""
        now = time.monotonic()
        with self._lock:
            items = self._items[client.key_id or "-"]
            while items and now - items[0] > 60:
                items.popleft()
            if len(items) + 1 + max(extra, 0) > ITEMS_PER_MINUTE:
                raise HisAuthError(f"ส่งเกิน {ITEMS_PER_MINUTE} รายการต่อนาที", 429)
            items.extend([now] * (1 + max(extra, 0)))

    # ------------------------------------------------------------- matching codes

    def _resolve(self, conn, kind: str, code: str | None, label: str | None) -> str | None:
        """Our staff_id / procedure_id for an HIS code (mapping table first)."""
        if code:
            row = conn.execute("SELECT target_id FROM his_mappings WHERE kind = %s AND his_code = %s",
                               (kind, code)).fetchone()
            if row:
                return row["target_id"]
        if kind == "surgeon":
            if code:
                row = conn.execute("""SELECT staff_id FROM staff WHERE lower(employee_code) = lower(%s)
                                      AND role = 'SURGEON' AND is_active""", (code,)).fetchone()
                if row:
                    return str(row["staff_id"])
            return None
        if code:
            row = conn.execute("SELECT procedure_id FROM procedures WHERE icd9_cm_code = %s", (code,)).fetchone()
            if row:
                return str(row["procedure_id"])
        if label:
            rows = [r for r in conn.execute("SELECT procedure_id, name FROM procedures") if _norm(r["name"]) == _norm(label)]
            if len(rows) == 1:
                return str(rows[0]["procedure_id"])
        return None

    @staticmethod
    def _suggest(kind: str, label: str | None, surgeons: list[dict], procs: list[dict]) -> str | None:
        """A likely match by name, offered to the admin (never applied by itself for doctors)."""
        if not label:
            return None
        if kind == "surgeon":
            rows = [s for s in surgeons if _person(s["name"]) and (_person(s["name"]) in _person(label)
                                                                    or _person(label) in _person(s["name"]))]
        else:
            rows = [p for p in procs if _norm(label) in _norm(p["name"])]
        return rows[0]["id"] if len(rows) == 1 else None

    # ------------------------------------------------------------- receiving

    def receive(self, payload: dict, *, source: str = "api", client: Client | None = None,
                actor: dict | None = None) -> dict:
        """One message from the HIS (or the test form); always answers, never half-applies."""
        store = self._db()
        who = f"HIS: {client.name}" if client else (actor or {}).get("name")
        with store._transaction(write=True) as conn:
            if not ready(conn):
                raise HisUnavailable(NEED_SQL)
            self._cleanup(conn)
            ref = CONTROL.sub("", str((payload or {}).get("his_ref") or ""))[:100] or None
            try:
                msg = normalize(payload)
            except StoreError as exc:
                out = {"status": "REJECTED", "detail": str(exc)}
                msg = None
            else:
                out = self._apply(conn, msg, who)
                # Older messages of this order still waiting for codes are out of date now: matching a
                # code later must not apply them over this one (or bring back a cancelled order).
                stale = conn.execute(
                    """UPDATE his_inbox SET status = 'UNCHANGED', payload = NULL,
                              detail = 'มีข้อความใหม่กว่าของเลขที่นี้แล้ว (ไม่ใช้ข้อความนี้)'
                       WHERE his_ref = %s AND status = 'NEEDS_MAPPING'""", (msg["his_ref"],)).rowcount
                if stale and msg["action"] == "cancel" and out["status"] == "REJECTED":
                    out = {"status": "APPLIED", "result": "cancelled",
                           "detail": "ยกเลิกแล้ว (คำขอนี้ยังรอจับคู่รหัส จึงยังไม่เคยเข้ารายการรอผ่าตัด)"}
            if out["status"] != "UNCHANGED":
                keep = out["status"] == "NEEDS_MAPPING" and msg is not None
                row = conn.execute(
                    """INSERT INTO his_inbox (source, key_id, kind, his_ref, status, detail, missing, request_id, payload)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING message_id""",
                    (source, uuid.UUID(client.key_id) if client and client.key_id else None,
                     f"request.{msg['action'] if msg else 'invalid'}", ref, out["status"], (out.get("detail") or "")[:DETAIL_MAX] or None,
                     Jsonb(out["missing"]) if out.get("missing") else None, out.get("request_id"),
                     Jsonb(_stored(msg)) if keep else None)).fetchone()
                out["message_id"] = row["message_id"]
        for event in out.pop("events", []):
            self._tell(event)
        out.pop("request_id", None)
        return {"his_ref": ref, **out, "status_label": STATUS_TH[out["status"]]}

    def _find(self, conn, ref: str, fields: str, world=None):
        """The request for an HIS order number, also after its identity was removed (hashed ``his_ref``)."""
        world = world or self._world(conn)
        anon = "r.anonymized_at" if world.has("surgery_requests", "anonymized_at") else "NULL::timestamptz AS anonymized_at"
        return conn.execute(
            f"""SELECT {fields}, {anon}
                FROM surgery_requests r
                LEFT JOIN scheduled_surgeries s ON s.request_id = r.request_id AND s.status IS DISTINCT FROM 'CANCELLED'
                LEFT JOIN operating_rooms o ON o.or_id = s.or_id
                WHERE r.his_ref = ANY(%s) ORDER BY r.his_ref = %s DESC LIMIT 1""",  # noqa: S608 (fixed field lists)
            ([ref, anon_ref(ref)], ref)).fetchone()

    def _apply(self, conn, msg: dict, who: str | None) -> dict:
        existing = self._find(conn, msg["his_ref"], """r.request_id, r.status, r.urgency_level, r.primary_surgeon_id,
                      r.procedure_id, r.patient_hn, r.patient_name, r.ai_predicted_duration_mins, r.due_date,
                      r.requested_date, s.start_time, s.actual_start_time, s.actual_end_time""")
        if existing is not None and existing["anonymized_at"] is not None:
            # Past the retention period: sending the order again must not bring the HN / name back.
            return {"code": case_code(existing["request_id"], existing["urgency_level"], set()),
                    "request_id": existing["request_id"], "status": "UNCHANGED", "result": "anonymized",
                    "detail": "คำขอนี้เกินระยะเวลาเก็บข้อมูลผู้ป่วยแล้ว (ลบข้อมูลระบุตัวตนแล้ว) ไม่เปลี่ยนข้อมูล"}
        if msg["action"] == "cancel":
            return self._cancel(conn, msg, existing, who)

        surgeon = self._resolve(conn, "surgeon", msg["surgeon_code"], msg["surgeon_name"])
        proc_key = msg["procedure_code"] or msg["procedure_name"]
        procedure = self._resolve(conn, "procedure", msg["procedure_code"], msg["procedure_name"])
        missing = ([{"kind": "surgeon", "his_code": msg["surgeon_code"], "label": msg["surgeon_name"]}] if not surgeon else []) \
            + ([{"kind": "procedure", "his_code": proc_key, "label": msg["procedure_name"]}] if not procedure else [])
        if missing:
            new_codes = [m for m in missing if not conn.execute(
                """SELECT 1 FROM his_inbox WHERE status = 'NEEDS_MAPPING' AND missing @> %s""",
                (Jsonb([{"kind": m["kind"], "his_code": m["his_code"]}]),)).fetchone()]
            what = ", ".join(f"{KINDS[m['kind']]} {m['his_code']}" + (f" ({m['label']})" if m["label"] else "") for m in missing)
            events = [{"type": "his_alert", "what": "mapping", "detail": "รหัสใหม่จาก HIS: " + what}] if new_codes else []
            return {"status": "NEEDS_MAPPING", "detail": "ยังจับคู่ไม่ได้: " + what, "missing": missing, "events": events}

        proc = conn.execute("""SELECT p.name, p.default_duration_mins, p.requires_icu_post_op, c.historical_avg_duration_mins AS card
                               FROM procedures p LEFT JOIN surgeon_preference_cards c
                                 ON c.procedure_id = p.procedure_id AND c.surgeon_id = %s
                               WHERE p.procedure_id = %s""", (uuid.UUID(surgeon), uuid.UUID(procedure))).fetchone()
        duration = msg["duration_mins"] or proc["card"] or proc["default_duration_mins"] or 60
        if existing is None:
            return self._create(conn, msg, surgeon, procedure, proc, duration, who)
        return self._update(conn, msg, existing, surgeon, procedure, duration, who)

    def _create(self, conn, msg, surgeon, procedure, proc, duration, who) -> dict:
        world = self._world(conn)
        request_id = uuid.uuid4()
        code = case_code(request_id, msg["urgency"], set())
        case = SimpleNamespace(case_id=code, patient_hn=msg["hn"], surgeon_id=surgeon, duration_mins=duration,
                               urgency=SimpleNamespace(value=msg["urgency"]), airborne=msg["airborne"])
        request = {"request_id": request_id, "patient_name": msg["patient_name"], "procedure_id": uuid.UUID(procedure),
                   "raw_text": proc["name"]}
        extra = {"his_ref": msg["his_ref"], "requested_date": msg["requested_date"]}
        due = msg["due_date"] if world.has("surgery_requests", "due_date") else None
        self.store.db.add_waiting(conn, world, case, request, due, extra=extra)
        self._log(conn, f"รับคำขอผ่าตัดจาก HIS {code} {proc['name']}", who, world)
        events = [{"type": "his_alert", "what": "emergency", "code": code, "procedure": proc["name"],
                   "detail": f"คำขอผ่าตัดฉุกเฉินจาก HIS: {code} {proc['name']}"}] if msg["urgency"] == "EMERGENCY" else []
        return {"status": "APPLIED", "detail": "สร้างคำขอใหม่ในรายการรอผ่าตัด", "code": code, "result": "created",
                "request_id": request_id, "events": events}

    def _update(self, conn, msg, existing, surgeon, procedure, duration, who) -> dict:
        code = case_code(existing["request_id"], existing["urgency_level"], set())
        wanted = {"primary_surgeon_id": uuid.UUID(surgeon), "procedure_id": uuid.UUID(procedure),
                  "urgency_level": msg["urgency"], "ai_predicted_duration_mins": duration, "patient_hn": msg["hn"],
                  "patient_name": msg["patient_name"], "due_date": msg["due_date"], "requested_date": msg["requested_date"]}
        changed = {k: v for k, v in wanted.items() if existing[k] != v}
        base = {"code": code, "request_id": existing["request_id"]}
        if existing["status"] == "CANCELLED":
            return {**base, "status": "UNCHANGED", "detail": "คำขอนี้ถูกยกเลิกแล้ว (ส่งเลขที่ใหม่ถ้าจะขอผ่าตัดอีกครั้ง)",
                    "result": "cancelled"}
        if not changed:
            return {**base, "status": "UNCHANGED", "detail": "ข้อมูลเหมือนเดิม", "result": "unchanged"}
        booked = existing["status"] != "PENDING"
        if booked:
            if existing["actual_start_time"] is not None:
                return {**base, "status": "UNCHANGED", "detail": "ผ่าตัดไปแล้ว ไม่เปลี่ยนข้อมูล", "result": "done"}
            soft = {k: v for k, v in changed.items() if k in ("due_date", "requested_date", "patient_name")}
            hard = [k for k in changed if k not in soft]
            if soft:
                self._set(conn, existing["request_id"], soft)
            if hard:
                fields = ", ".join(sorted({"primary_surgeon_id": "ศัลยแพทย์", "procedure_id": "หัตถการ", "urgency_level": "ความเร่งด่วน",
                                           "ai_predicted_duration_mins": "ระยะเวลา", "patient_hn": "HN"}[k] for k in hard))
                detail = f"เคสจองแล้ว: HIS เปลี่ยน{fields} ผู้จัดตารางต้องแก้ในระบบเอง (งดแล้วจองใหม่)"
                self._log(conn, f"HIS แก้คำขอ {code} ที่จองแล้ว ({fields}) ต้องดำเนินการ", who)
                return {**base, "status": "NEEDS_ACTION", "detail": detail, "result": "booked",
                        "events": [{"type": "his_alert", "what": "action", "code": code, "detail": detail}]}
            return {**base, "status": "APPLIED", "detail": "แก้วันที่ของคำขอที่จองแล้ว", "result": "updated"}
        self._set(conn, existing["request_id"], changed)
        self._log(conn, f"HIS แก้คำขอ {code} ในรายการรอผ่าตัด", who)
        return {**base, "status": "APPLIED", "detail": "แก้คำขอในรายการรอผ่าตัด", "result": "updated"}

    @staticmethod
    def _set(conn, request_id, fields: dict) -> None:
        cols = ", ".join(f"{k} = %s" for k in fields)  # keys come from a fixed list
        conn.execute(f"UPDATE surgery_requests SET {cols} WHERE request_id = %s", [*fields.values(), request_id])  # noqa: S608

    def _cancel(self, conn, msg, existing, who) -> dict:
        if existing is None:
            return {"status": "REJECTED", "detail": "ไม่พบคำขอเลขที่นี้"}
        code = case_code(existing["request_id"], existing["urgency_level"], set())
        base = {"code": code, "request_id": existing["request_id"]}
        if existing["status"] == "CANCELLED":
            return {**base, "status": "UNCHANGED", "detail": "ยกเลิกไปแล้ว", "result": "cancelled"}
        if existing["status"] == "PENDING":
            db = self.store.db
            db.cancel_request(conn, existing["request_id"])
            db.record_given_up(conn, existing["request_id"], code, "HIS", None, who)
            self._log(conn, f"HIS ยกเลิกคำขอ {code}", who)
            return {**base, "status": "APPLIED", "detail": "ยกเลิกคำขอในรายการรอผ่าตัดแล้ว", "result": "cancelled"}
        if existing["actual_start_time"] is not None:
            return {**base, "status": "UNCHANGED", "detail": "ผ่าตัดไปแล้ว ยกเลิกไม่ได้", "result": "done"}
        when = existing["start_time"].astimezone(self.store.db.tz).strftime("%d/%m %H:%M") if existing["start_time"] else ""
        detail = f"เคสจองแล้ว {when}: HIS ยกเลิกคำขอ ผู้จัดตารางต้องกดงดผ่าตัดเอง"
        self._log(conn, f"HIS ยกเลิกคำขอ {code} ที่จองแล้ว {when} ต้องงดผ่าตัดในระบบ", who)
        return {**base, "status": "NEEDS_ACTION", "detail": detail, "result": "booked",
                "events": [{"type": "his_alert", "what": "action", "code": code, "detail": detail}]}

    def retry(self, message_id: int, actor: dict | None = None) -> dict:
        """Apply a message again (after its codes were matched)."""
        with self._db()._transaction(write=True) as conn:
            if not ready(conn):
                raise HisUnavailable(NEED_SQL)
            purge_inbox(conn)  # a message past INBOX_KEEP_DAYS has no data left to apply
            row = conn.execute("SELECT payload, status FROM his_inbox WHERE message_id = %s FOR UPDATE",
                               (message_id,)).fetchone()
            if row is None or row["payload"] is None or row["status"] != "NEEDS_MAPPING":
                raise StoreError("ข้อความนี้ประมวลผลซ้ำไม่ได้ (ไม่มีข้อมูลเหลือ หรือทำไปแล้ว)")
            out = self._apply(conn, normalize(row["payload"]), (actor or {}).get("name"))
            conn.execute("""UPDATE his_inbox SET status = %s, detail = %s, missing = %s, request_id = %s,
                                   payload = CASE WHEN %s = 'NEEDS_MAPPING' THEN payload END
                            WHERE message_id = %s""",
                         (out["status"], (out.get("detail") or "")[:DETAIL_MAX] or None,
                          Jsonb(out["missing"]) if out.get("missing") else None,
                          out.get("request_id"), out["status"], message_id))
        for event in out.pop("events", []):
            if event.get("what") != "mapping":
                self._tell(event)
        out.pop("request_id", None)
        return {**out, "status_label": STATUS_TH[out["status"]]}

    @staticmethod
    def _cleanup(conn) -> None:
        purge_inbox(conn)

    # ------------------------------------------------------------- mapping (admin)

    def set_mapping(self, kind: str, his_code: str, target_id: str, label: str | None = None,
                    actor: dict | None = None) -> dict:
        if kind not in KINDS:
            raise StoreError("ชนิดการจับคู่ไม่ถูกต้อง")
        his_code = str(his_code or "").strip()[:100]
        if not his_code:
            raise StoreError("ต้องมีรหัสของ HIS")
        try:
            target = uuid.UUID(str(target_id))
        except ValueError as exc:
            raise StoreError("เลือกรายการในระบบที่จะจับคู่") from exc
        with self._db()._transaction(write=True) as conn:
            if not ready(conn):
                raise HisUnavailable(NEED_SQL)
            sql = ("SELECT full_name AS name FROM staff WHERE staff_id = %s AND role = 'SURGEON'" if kind == "surgeon"
                   else "SELECT name FROM procedures WHERE procedure_id = %s")
            found = conn.execute(sql, (target,)).fetchone()
            if found is None:
                raise StoreError("ไม่พบรายการที่เลือกในระบบ")
            conn.execute("""INSERT INTO his_mappings (kind, his_code, target_id, his_label, updated_by)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (kind, his_code) DO UPDATE SET target_id = EXCLUDED.target_id,
                                his_label = COALESCE(EXCLUDED.his_label, his_mappings.his_label),
                                updated_by = EXCLUDED.updated_by, updated_at = NOW()""",
                         (kind, his_code, str(target), (label or "")[:200] or None, (actor or {}).get("name")))
            self._log(conn, f"จับคู่รหัส HIS {KINDS[kind]} {his_code} → {found['name']}", (actor or {}).get("name"))
            purge_inbox(conn)
            waiting = [r["message_id"] for r in conn.execute(
                """SELECT message_id FROM his_inbox WHERE status = 'NEEDS_MAPPING' AND payload IS NOT NULL
                   AND missing @> %s ORDER BY message_id""", (Jsonb([{"kind": kind, "his_code": his_code}]),))]
        results = []
        for message_id in waiting:  # messages that waited for this code
            try:
                results.append(self.retry(message_id, actor)["status"])
            except StoreError:
                continue
        return {"kind": kind, "his_code": his_code, "target_id": str(target), "retried": len(results),
                "applied": sum(1 for s in results if s == "APPLIED")}

    def delete_mapping(self, kind: str, his_code: str, actor: dict | None = None) -> dict:
        with self._db()._transaction(write=True) as conn:
            if not ready(conn):
                raise HisUnavailable(NEED_SQL)
            if conn.execute("DELETE FROM his_mappings WHERE kind = %s AND his_code = %s", (kind, his_code)).rowcount == 0:
                raise StoreError("ไม่พบการจับคู่นี้")
            self._log(conn, f"ลบการจับคู่รหัส HIS {KINDS.get(kind, kind)} {his_code}", (actor or {}).get("name"))
        return {"deleted": his_code}

    # ------------------------------------------------------------- reading back (HIS)

    def request_status(self, his_ref: str, client: Client, address: str | None = None) -> dict:
        if not his_ref or len(his_ref) > 100 or CONTROL.search(his_ref) or his_ref.startswith(ANON_PREFIX):
            raise StoreError("ไม่พบคำขอเลขที่นี้")
        store = self._db()
        with store._transaction(write=False) as conn:
            if not ready(conn):
                raise HisUnavailable(NEED_SQL)
            row = self._find(conn, his_ref, """r.request_id, r.status, r.urgency_level, r.due_date, s.start_time,
                          s.end_time, s.actual_start_time, s.actual_end_time, o.room_code""")
        if row is None:
            raise StoreError("ไม่พบคำขอเลขที่นี้")
        out = {"his_ref": his_ref, "code": case_code(row["request_id"], row["urgency_level"], set()), **self._state(row)}
        if self.access is not None and out.get("date"):
            self.access.record(client.user(), "his", out["date"], [out["code"]], address)
        return out

    def _state(self, row) -> dict:
        tz = self.store.db.tz
        if row["status"] == "CANCELLED":
            return {"status": "cancelled"}
        if row["start_time"] is None:
            return {"status": "waiting", "due_date": row["due_date"].isoformat() if row.get("due_date") else None}
        status = "done" if row["actual_end_time"] else "in_progress" if row["actual_start_time"] else "booked"
        start, end = row["start_time"].astimezone(tz), row["end_time"].astimezone(tz)
        return {"status": status, "date": start.date().isoformat(), "start": start.strftime("%H:%M"),
                "end": end.strftime("%H:%M"), "room": row["room_code"]}

    def schedule(self, day: str | None, client: Client, address: str | None = None) -> dict:
        store = self._db()
        target = _day(day, "date") if day else store.db.today()
        tz = store.db.tz
        start = datetime(target.year, target.month, target.day, tzinfo=tz)
        with store._transaction(write=False) as conn:
            if not ready(conn):
                raise HisUnavailable(NEED_SQL)
            codes = {r["target_id"]: r["his_code"] for r in conn.execute(
                "SELECT target_id, his_code FROM his_mappings WHERE kind = 'surgeon'")}
            rows = conn.execute(
                """SELECT r.request_id, r.his_ref, r.urgency_level, r.patient_hn, r.status, s.start_time, s.end_time,
                          s.actual_start_time, s.actual_end_time, o.room_code, p.name AS procedure, p.icd9_cm_code,
                          st.staff_id AS surgeon_id, st.full_name AS surgeon, st.employee_code
                   FROM scheduled_surgeries s JOIN surgery_requests r ON r.request_id = s.request_id
                   JOIN operating_rooms o ON o.or_id = s.or_id
                   LEFT JOIN procedures p ON p.procedure_id = r.procedure_id
                   LEFT JOIN staff st ON st.staff_id = r.primary_surgeon_id
                   WHERE s.status IS DISTINCT FROM 'CANCELLED' AND s.start_time >= %s AND s.start_time < %s
                   ORDER BY o.room_code, s.start_time""", (start, start + timedelta(days=1))).fetchall()
        cases = [{
            "code": case_code(r["request_id"], r["urgency_level"], set()),
            "his_ref": None if (r["his_ref"] or "").startswith(ANON_PREFIX) else r["his_ref"], "hn": r["patient_hn"],
            "urgency": r["urgency_level"], "procedure": r["procedure"], "procedure_code": r["icd9_cm_code"],
            "surgeon": r["surgeon"], "surgeon_code": codes.get(str(r["surgeon_id"])) or r["employee_code"],
            **self._state(r)} for r in rows]
        if self.access is not None and cases:
            self.access.record(client.user(), "his", target.isoformat(), [c["code"] for c in cases], address)
        return {"date": target.isoformat(), "cases": cases}

    # ------------------------------------------------------------- admin page

    def overview(self) -> dict:
        store = self._db()
        with store._transaction(write=False) as conn:
            if not ready(conn):
                return {"ready": False, "scopes": SCOPES}
            keys = [{"id": str(r["key_id"]), "name": r["name"], "prefix": r["prefix"], "scopes": list(r["scopes"] or []),
                     "created_by": r["created_by"], "created_at": r["created_at"].isoformat(),
                     "last_used_at": r["last_used_at"].isoformat() if r["last_used_at"] else None,
                     "revoked": r["revoked_at"] is not None}
                    for r in conn.execute("SELECT * FROM his_api_keys ORDER BY revoked_at NULLS FIRST, created_at DESC")]
            surgeons = [{"id": str(r["staff_id"]), "name": r["full_name"], "code": r["employee_code"]} for r in conn.execute(
                "SELECT staff_id, full_name, employee_code FROM staff WHERE role = 'SURGEON' AND is_active ORDER BY full_name")]
            procs = [{"id": str(r["procedure_id"]), "name": r["name"], "code": r["icd9_cm_code"]} for r in conn.execute(
                "SELECT procedure_id, name, icd9_cm_code FROM procedures ORDER BY name")]
            names = {**{s["id"]: s["name"] for s in surgeons}, **{p["id"]: p["name"] for p in procs}}
            mappings = [{"kind": r["kind"], "kind_label": KINDS[r["kind"]], "his_code": r["his_code"], "his_label": r["his_label"],
                         "target_id": r["target_id"], "target_name": names.get(r["target_id"], "(ไม่พบแล้ว)"),
                         "updated_by": r["updated_by"], "updated_at": r["updated_at"].isoformat()}
                        for r in conn.execute("SELECT * FROM his_mappings ORDER BY kind, his_code")]
            unmapped: dict[tuple, dict] = {}
            for r in conn.execute("""SELECT missing FROM his_inbox WHERE status = 'NEEDS_MAPPING' AND payload IS NOT NULL
                                      AND received_at >= NOW() - make_interval(days => %s)
                                      ORDER BY message_id DESC LIMIT 2000""", (INBOX_KEEP_DAYS,)):
                for m in r["missing"] or []:
                    item = unmapped.setdefault((m["kind"], m["his_code"]), {**m, "kind_label": KINDS[m["kind"]], "messages": 0})
                    item["messages"] += 1
            unmapped = dict(sorted(unmapped.items())[:200])  # a page the admin can work through
            for item in unmapped.values():
                item["suggestion"] = self._suggest(item["kind"], item.get("label") or item["his_code"], surgeons, procs)
            key_names = {k["id"]: k["name"] for k in keys}
            inbox = [{"id": r["message_id"], "received_at": r["received_at"].isoformat(), "source": r["source"],
                      "key": key_names.get(str(r["key_id"])) if r["key_id"] else None, "kind": r["kind"],
                      "his_ref": r["his_ref"], "status": r["status"], "status_label": STATUS_TH[r["status"]],
                      "detail": r["detail"],
                      "code": case_code(r["request_id"], r["urgency_level"], set()) if r["urgency_level"] else None,
                      "retry": r["status"] == "NEEDS_MAPPING" and r["has_payload"]}
                     for r in conn.execute("""SELECT i.message_id, i.received_at, i.source, i.key_id, i.kind, i.his_ref,
                                                     i.status, i.detail, i.request_id, r.urgency_level,
                                                     i.payload IS NOT NULL AND i.received_at >= NOW()
                                                         - make_interval(days => %s) AS has_payload
                                              FROM his_inbox i LEFT JOIN surgery_requests r ON r.request_id = i.request_id
                                              ORDER BY i.message_id DESC LIMIT 50""", (INBOX_KEEP_DAYS,))]
            counts = {r["status"]: r["n"] for r in conn.execute(
                """SELECT status, count(*) AS n FROM his_inbox WHERE received_at > NOW() - INTERVAL '7 days' GROUP BY status""")}
        return {"ready": True, "scopes": SCOPES, "kinds": KINDS, "keys": keys, "mappings": mappings,
                "unmapped": sorted(unmapped.values(), key=lambda m: (m["kind"], m["his_code"])), "inbox": inbox,
                "counts": counts, "targets": {"surgeon": surgeons, "procedure": procs}}

    def check(self) -> dict | None:
        """For the go-live checklist: only once a key exists."""
        if getattr(self.store, "mode", "demo") != "supabase":
            return None
        try:
            with self.store._transaction(write=False) as conn:
                if not ready(conn):
                    return None
                keys = conn.execute("""SELECT count(*) AS n, max(last_used_at) AS used FROM his_api_keys
                                       WHERE revoked_at IS NULL""").fetchone()
                if not keys["n"]:
                    return None
                open_ = {r["status"]: r["n"] for r in conn.execute(
                    """SELECT status, count(*) AS n FROM his_inbox WHERE received_at > NOW() - INTERVAL '7 days'
                       AND status IN ('NEEDS_MAPPING', 'NEEDS_ACTION', 'REJECTED') GROUP BY status""")}
        except Exception:
            log.warning("HIS check failed", exc_info=True)
            return None
        problems = [f"{STATUS_TH[s]} {n}" for s, n in sorted(open_.items())]
        used = keys["used"].astimezone(self.store.db.tz).strftime("%d/%m %H:%M") if keys["used"] else "ยังไม่เคยเรียก"
        return {"label": "การเชื่อมต่อ HIS", "status": "warn" if problems else "ok",
                "detail": (" · ".join(problems) + " (7 วัน)" if problems else "ไม่มีข้อความค้าง") + f" · HIS เรียกล่าสุด {used}",
                "link": "/admin#his"}
