"""Hospital master data from the HIS in HL7 FHIR R4: staff and their rosters, operating rooms, equipment.

The HIS is the source of these records and ORchestraAI keeps its own working data on them:

  FHIR R4                           here               the HIS owns (locked here)        the OR team owns
  Location (an OR, by its type)     operating_rooms    room code, open / closed           capabilities, cleaning
                                                                                          times, elective hours
  Practitioner + PractitionerRole   staff              employee code, name, role,         skills, hour limits
  (OR roles only)                                      active
  Device (OR equipment, by type)    equipment_units    serial / asset no., type, retired  CSSD status, where it is
  Schedule + Slot                   staff_schedules    the shifts it sent                 shifts added in the app

A record or shift from the HIS carries ``managed_by``; the app refuses to change what the HIS owns
(app/admin_store.py), in its forms and in an Excel import alike. Room type and specialty come from the
HIS when a record is created and are the OR team's afterwards.

Three ways in, one pipeline (``MasterData.run``): the HIS pushes a Bundle (``POST /api/his/v1/fhir``),
the app pulls from the HIS's FHIR server (``pull``, by hand or every few hours), or an admin uploads a
Bundle file. Every run can be a trial (dry run): it does all the work and then rolls back, so its report
is exactly what a real run would do. Records are matched by their code (rooms by room code, staff by
employee code, units by serial), so the same data sent again changes nothing. A resource that fails is
reported and the others are saved. Rows go through the same checks as the Excel import
(app/hospital_io.py). Only what the OR needs is taken: locations that are not ORs, practitioners
without an OR role and devices of other types are counted as skipped. No patient data comes this way.

Rosters: for each person and day the HIS sends (the days of its slots, and every day of a Schedule's
planning horizon), its shifts are the HIS's shifts for that day. Shifts it sent before and no longer
sends are removed; an identical shift typed in the app before is taken over instead of doubled; shifts
added in the app stay. Supabase mode only (migration 019).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time as clock
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from .admin_store import CODE, ROLES, ROOM_TYPES, SHIFT_TYPES, HospitalAdmin
from .hospital_io import ROOMS, STAFF, UNITS, Table, _Run, current
from .store import StoreError

log = logging.getLogger("orchestra.his_fhir")

SOURCE = "his"  # managed_by of what came from the HIS
SETTINGS_KEY = "his_fhir"
SCOPE = "masterdata:write"
NEED_SQL = "ยังรับข้อมูลหลักจาก HIS ไม่ได้: ต้องรัน SQL ไฟล์ 019 ก่อน (หน้า ⚙ สถานะระบบ → รัน SQL ที่ยังขาด)"
MAX_RESOURCES = 20000
MAX_PROBLEMS = 200  # listed in a report (all are counted)
RESOURCE_TYPES = ("Location", "Practitioner", "PractitionerRole", "Device", "Schedule", "Slot")
PULL_ORDER = RESOURCE_TYPES  # rooms and people before the rosters and devices that point at them
ROSTER_PAST_DAYS, ROSTER_FUTURE_DAYS, ROSTER_MAX_DAYS = 62, 400, 93
TOKEN_ENV = re.compile(r"^HIS_[A-Z0-9_]{1,60}$")
PAGE_LIMIT = 200          # pages per resource type in one pull
RESPONSE_MAX = 25 * 1024 * 1024
TIMEOUT = (10, 30)        # connect, read (seconds)
SHIFT_KINDS = (*SHIFT_TYPES, "OFF")
TYPE_TH = {"REGULAR": "เวรปกติ", "ON_CALL": "on-call", "LEAVE": "ลา"}
ENTITY_TH = {"room": "ห้องผ่าตัด", "staff": "บุคลากร", "equipment": "เครื่องมือ", "roster": "ตารางเวร"}
LINK = {"room": ("operating_rooms", "room_code"), "staff": ("staff", "employee_code"),
        "equipment": ("equipment_units", "serial_number")}

DEFAULTS = {
    "base_url": "",          # the HIS's FHIR R4 server, for pulling (empty: the HIS pushes, or files)
    "token_env": "",         # name of the environment variable that holds the token (never the token)
    "pull_hours": 0,         # pull every N hours (0 = only when an admin presses the button)
    "roster_days": 31,       # a pull takes the rosters of the coming N days (and yesterday)
    # Location.type code -> room type: only locations with one of these codes are operating rooms.
    "room_types": {"OR": "STANDARD", "OR-HYBRID": "HYBRID", "OR-NEG": "NEGATIVE_PRESSURE"},
    # PractitionerRole code / specialty or qualification code -> role: people with none are not OR staff.
    "roles": {r: r for r in ROLES},
    # Device.type code -> equipment type (by its name here). Types whose name equals the code match too.
    "equipment_types": {},
    # Slot.serviceType / appointmentType code -> shift type (OFF = not a shift). Without one: free =
    # REGULAR, busy-unavailable = LEAVE.
    "shift_types": {"REGULAR": "REGULAR", "ON_CALL": "ON_CALL", "ONCALL": "ON_CALL", "LEAVE": "LEAVE", "OFF": "OFF"},
    # Identifier systems (optional): which identifier is the employee code, license, asset no., room code.
    "systems": {"employee": "", "license": "", "asset": "", "room": ""},
}
MAP_VALUES = {"room_types": ROOM_TYPES, "roles": ROLES, "shift_types": SHIFT_KINDS, "equipment_types": None}


class Skip(Exception):
    """A resource that is not for the operating room (not an error)."""


# ------------------------------------------------------------------ reading FHIR

def _codings(value) -> list[dict]:
    items = value if isinstance(value, list) else [value] if value else []
    return [c for cc in items if isinstance(cc, dict) for c in (cc.get("coding") or []) if isinstance(c, dict)]


def _codes(value) -> list[str]:
    return [str(c.get("code")).strip() for c in _codings(value) if c.get("code")]


def _text(value) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or None


def _cc_text(value) -> str | None:
    """A CodeableConcept as words: its text, else the first coding's display."""
    items = value if isinstance(value, list) else [value] if value else []
    for cc in items:
        if isinstance(cc, dict):
            text = _text(cc.get("text")) or next((_text(c.get("display")) for c in cc.get("coding") or []
                                                  if isinstance(c, dict) and c.get("display")), None)
            if text:
                return text
    return None


def _identifier(r: dict, system: str | None = None, types: tuple = (), first: bool = True) -> str | None:
    """The identifier from ``system``, else one of the ``types`` (v2-0203 codes), else the first one."""
    ids = [i for i in r.get("identifier") or [] if isinstance(i, dict) and _text(i.get("value"))]
    if system:
        hit = next((i for i in ids if i.get("system") == system), None)
        if hit:
            return _text(hit["value"])
    hit = next((i for i in ids if set(_codes(i.get("type"))) & set(types)), None)
    if hit:
        return _text(hit["value"])
    return _text(ids[0]["value"]) if first and ids else None


def _ref(value) -> str | None:
    """"Practitioner/123" from a Reference (relative or absolute URL)."""
    if not isinstance(value, dict):
        return None
    ref = str(value.get("reference") or "").strip()
    if not ref or ref.startswith("#"):
        return None
    parts = [p for p in ref.split("/") if p]
    if "_history" in parts:
        parts = parts[:parts.index("_history")]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def _key(r: dict) -> str:
    return f"{r.get('resourceType')}/{r.get('id') or '?'}"


def human_name(names) -> str | None:
    names = [n for n in names or [] if isinstance(n, dict)]
    n = next((x for x in names if x.get("use") == "official"), names[0] if names else None)
    if n is None:
        return None
    if _text(n.get("text")):
        return _text(n["text"])
    prefix = "".join(_text(p) or "" for p in n.get("prefix") or [])
    given = " ".join(_text(g) or "" for g in n.get("given") or []).strip()
    full = " ".join(p for p in (prefix + given if prefix and given else prefix or given, _text(n.get("family"))) if p)
    return _text(full)


def _when(value: str | None, tz) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time(), tzinfo=tz)
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=tz)


def resources_of(payload) -> list[dict]:
    """The resources of a Bundle (its entries), one resource, or a list of either."""
    out: list[dict] = []
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            if item.get("resourceType") == "Bundle":
                stack.extend(reversed([e.get("resource") for e in item.get("entry") or [] if isinstance(e, dict)]))
            elif item.get("resourceType"):
                out.append(item)
        if len(out) > MAX_RESOURCES:
            raise StoreError(f"ส่งได้ครั้งละไม่เกิน {MAX_RESOURCES:,} รายการ กรุณาแบ่งส่ง")
    return out


def parse_text(text: str) -> list[dict]:
    try:
        return resources_of(json.loads(text))
    except json.JSONDecodeError as exc:
        raise StoreError(f"ไฟล์ไม่ใช่ JSON ที่ถูกต้อง (บรรทัด {exc.lineno}): ต้องเป็น FHIR Bundle แบบ JSON") from None
    except RecursionError:
        raise StoreError("ไฟล์ซ้อนกันลึกเกินไป ไม่ใช่ FHIR Bundle") from None


# ------------------------------------------------------------------ settings

def _clean_map(name: str, raw) -> dict:
    if not isinstance(raw, dict):
        raise StoreError(f"{name} ต้องเป็นรายการ รหัส = ค่า")
    allowed = MAP_VALUES[name]
    out = {}
    for code, value in list(raw.items())[:300]:
        code, value = _text(code), _text(value)
        if not code or not value:
            continue
        if len(code) > 100 or len(value) > 150:
            raise StoreError(f"{name}: รหัสหรือค่ายาวเกินไป ({code[:30]})")
        if allowed is not None:
            value = value.upper()
            if value not in allowed:
                raise StoreError(f"{name}: {code} = {value} ไม่ถูกต้อง (ใช้ได้ {', '.join(allowed)})")
        out[code] = value
    return out


def clean_settings(data: dict) -> dict:
    """What an admin may save (checked, nothing unknown kept)."""
    out = {}
    url = (data.get("base_url") or "").strip().rstrip("/")
    if url:
        check_base_url(url)
    out["base_url"] = url
    env = (data.get("token_env") or "").strip().upper()
    if env and not TOKEN_ENV.match(env):
        raise StoreError("ชื่อตัวแปร token ต้องขึ้นต้นด้วย HIS_ แล้วตามด้วย A-Z 0-9 _ เช่น HIS_TOKEN "
                         "(ใส่ token จริงที่ Railway → Variables ไม่ใช่ที่นี่)")
    out["token_env"] = env
    try:
        out["pull_hours"] = int(data.get("pull_hours") or 0)
        out["roster_days"] = int(data.get("roster_days") or DEFAULTS["roster_days"])
    except (TypeError, ValueError):
        raise StoreError("ช่วงเวลาดึงข้อมูลต้องเป็นตัวเลข") from None
    if not 0 <= out["pull_hours"] <= 168:
        raise StoreError("ดึงอัตโนมัติทุก 0-168 ชั่วโมง (0 = ไม่ดึงเอง)")
    if not 1 <= out["roster_days"] <= 62:
        raise StoreError("ดึงตารางเวรล่วงหน้าได้ 1-62 วัน")
    for name in MAP_VALUES:
        out[name] = _clean_map(name, data.get(name, DEFAULTS[name]))
    systems = data.get("systems") or {}
    out["systems"] = {k: (_text(systems.get(k)) or "")[:200] for k in DEFAULTS["systems"]}
    return out


def check_base_url(url: str) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    local = host in ("localhost", "127.0.0.1", "::1")
    if parts.scheme not in ("https", "http") or not host or parts.username or parts.password or parts.query \
            or parts.fragment or len(url) > 300:
        raise StoreError("ที่อยู่ FHIR server ต้องเป็น https://… (ไม่มี ? # หรือชื่อผู้ใช้)")
    if parts.scheme == "http" and not local:
        raise StoreError("ที่อยู่ FHIR server ต้องเป็น https:// (ข้อมูลบุคลากรต้องเข้ารหัสระหว่างทาง)")


# ------------------------------------------------------------------ mapping (no database)

def map_location(r: dict, settings: dict) -> dict:
    """An operating room, or Skip (a ward, a bed, a floor ...)."""
    codes = _codes(r.get("physicalType"))
    if "bd" in codes:
        raise Skip("เตียง (ยังไม่รับ)")
    room_type = next((settings["room_types"][c] for c in _codes(r.get("type")) if c in settings["room_types"]), None)
    if room_type is None:
        raise Skip("ไม่ใช่ห้องผ่าตัด")
    code = _identifier(r, settings["systems"].get("room")) or next((_text(a) for a in r.get("alias") or [] if _text(a)), None) \
        or _text(r.get("name")) or r.get("id")
    if not code or not CODE.match(code):
        raise StoreError(f"รหัสห้อง {code!r} ใช้ไม่ได้ (1-20 ตัว A-Z 0-9 - _ . ไม่มีช่องว่าง) ตั้ง identifier ของห้องให้เป็นรหัสสั้น")
    status = {"inactive": "CLOSED", "suspended": "MAINTENANCE"}.get(r.get("status"), "AVAILABLE")
    if (r.get("operationalStatus") or {}).get("code") == "C":  # v2-0116 Closed
        status = "CLOSED"
    return {"code": code, "type": room_type, "status": status, "name": _text(r.get("name"))}


def location_label(r: dict) -> str | None:
    return _text(r.get("name")) or next((_text(a) for a in r.get("alias") or [] if _text(a)), None) or _identifier(r)


def map_practitioner(r: dict, roles: list[dict], settings: dict) -> dict:
    """Staff, or Skip when none of the person's roles (or qualifications) is an OR role."""
    systems = settings["systems"]
    active_roles = [x for x in roles if x.get("active") is not False]
    codes = [c for x in active_roles for c in _codes(x.get("code")) + _codes(x.get("specialty"))]
    codes += [c for q in r.get("qualification") or [] if isinstance(q, dict) for c in _codes(q.get("code"))]
    role = next((settings["roles"][c] for c in codes if c in settings["roles"]), None)
    if role is None:
        raise Skip("ไม่ใช่บุคลากรห้องผ่าตัด (ไม่มีบทบาทที่ตั้งจับคู่ไว้)")
    license_no = _identifier(r, systems.get("license"), ("PRN", "LN", "MD"), first=False)
    code = _identifier(r, systems.get("employee"), ("EI",), first=False) or license_no \
        or _identifier(r) or f"HIS-{r.get('id')}"
    name = human_name(r.get("name"))
    if not name:
        raise StoreError("ไม่มีชื่อ (Practitioner.name)")
    specialty = next((_cc_text(x.get("specialty")) for x in active_roles if _cc_text(x.get("specialty"))), None)
    active = r.get("active") is not False and (not roles or bool(active_roles))
    return {"code": code[:50], "name": name[:150], "role": role, "active": active, "specialty": specialty}


def map_device(r: dict, settings: dict, type_names: dict[str, str]) -> dict:
    """An equipment unit, or Skip when its type is not OR equipment here.

    ``type_names``: lower-case name -> name of the equipment types stored here."""
    systems, wanted = settings["systems"], settings["equipment_types"]
    type_name = None
    for c in _codings(r.get("type")):
        code = str(c.get("code") or "").strip()
        hit = wanted.get(f"{c.get('system')}|{code}") or wanted.get(code)
        if hit:
            type_name = type_names.get(hit.lower())
            if type_name is None:
                raise StoreError(f"ตั้งจับคู่ {code} = {hit} ไว้ แต่ยังไม่มีชนิดเครื่องมือชื่อ {hit} (เพิ่มในแท็บเครื่องมือก่อน)")
            break
    if type_name is None:
        words = [str(c.get(k) or "").strip().lower() for c in _codings(r.get("type")) for k in ("code", "display")]
        words.append(str((r.get("type") or {}).get("text") or "").strip().lower() if isinstance(r.get("type"), dict) else "")
        type_name = next((type_names[w] for w in words if w and w in type_names), None)
    if type_name is None:
        raise Skip("ไม่ใช่เครื่องมือห้องผ่าตัด (ชนิดไม่ตรงกับที่ตั้งจับคู่ไว้)")
    serial = _identifier(r, systems.get("asset"), first=False) or _text(r.get("serialNumber")) or _identifier(r) \
        or r.get("id")
    if not serial or len(serial) > 100:
        raise StoreError("ไม่มีหมายเลขครุภัณฑ์ / serial (หรือยาวเกิน 100 ตัว)")
    return {"serial": serial, "type": type_name, "retired": r.get("status") in ("inactive", "entered-in-error"),
            "location": _ref(r.get("location")), "location_text": _text((r.get("location") or {}).get("display"))}


def map_slot(r: dict, settings: dict, tz) -> tuple[str, datetime, datetime, str | None] | None:
    """(type, start, end, note) or None (not a shift: OFF, cancelled)."""
    if r.get("status") == "entered-in-error":
        return None
    start, end = _when(r.get("start"), tz), _when(r.get("end"), tz)
    if start is None or end is None:
        raise StoreError("Slot ต้องมี start และ end")
    if end <= start or end - start > timedelta(hours=36):
        raise StoreError("เวลาเวรไม่ถูกต้อง (end ต้องหลัง start และยาวไม่เกิน 36 ชั่วโมง)")
    codes = _codes(r.get("serviceType")) + _codes(r.get("appointmentType")) + _codes(r.get("serviceCategory"))
    kind = next((settings["shift_types"][c] for c in codes if c in settings["shift_types"]), None)
    if kind is None:
        kind = "LEAVE" if r.get("status") == "busy-unavailable" else "REGULAR"
    if kind == "OFF":
        return None
    note = (_text(r.get("comment")) or "")[:200] or None
    return kind, start, end, note if kind == "LEAVE" else None


# ------------------------------------------------------------------ the pipeline

class MasterData:
    def __init__(self, store) -> None:
        self.store = store
        self._busy = threading.Lock()  # one sync at a time (they all take the write lock anyway)

    def _admin(self) -> HospitalAdmin:
        from .admin_store import require_supabase

        return require_supabase(self.store)

    # --------------------------------------------------------- settings

    def ready(self, conn) -> bool:
        return conn.execute("SELECT to_regclass('public.his_sync_runs') IS NOT NULL AS ok").fetchone()["ok"]

    def settings(self) -> dict:
        admin = self._admin()
        with admin.store._transaction(write=False) as conn:
            return self._settings(conn)

    def _settings(self, conn) -> dict:
        row = None
        if conn.execute("SELECT to_regclass('public.app_settings') IS NOT NULL AS ok").fetchone()["ok"]:
            row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (SETTINGS_KEY,)).fetchone()
        saved = {}
        if row:
            try:
                saved = json.loads(row["value"])
            except (TypeError, ValueError):
                saved = {}
        out = {**json.loads(json.dumps(DEFAULTS)), **{k: v for k, v in saved.items() if k in DEFAULTS}}
        out["systems"] = {**DEFAULTS["systems"], **(saved.get("systems") or {})}
        out["cursors"] = saved.get("cursors") or {}
        return out

    def _write_settings(self, conn, settings: dict) -> None:
        conn.execute("""INSERT INTO app_settings (key, value) VALUES (%s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                     (SETTINGS_KEY, json.dumps(settings, ensure_ascii=False)))

    def save_settings(self, data: dict, actor: dict | None = None) -> dict:
        clean = clean_settings(data)
        admin = self._admin()
        with admin.store._transaction(write=True) as conn:
            if not self.ready(conn):
                raise StoreError(NEED_SQL)
            old = self._settings(conn)
            clean["cursors"] = old.get("cursors", {}) if clean["base_url"] == old.get("base_url") else {}
            self._write_settings(conn, clean)
            admin._log(conn, "ตั้งค่ารับข้อมูลหลักจาก HIS (FHIR)",
                       [f"FHIR server: {clean['base_url'] or '-'} · ดึงเองทุก {clean['pull_hours']} ชม."
                        if clean["pull_hours"] else f"FHIR server: {clean['base_url'] or '-'} · ดึงเมื่อกดปุ่ม"], actor)
        return clean

    # --------------------------------------------------------- one run

    def run(self, payload, *, mode: str, dry_run: bool, actor: dict | None = None, by: str | None = None,
            window: tuple[date, date] | None = None) -> dict:
        """Apply resources (a Bundle, a resource or a list) and keep a record of the run.

        ``window``: the days a pull asked rosters for (every day of it is the HIS's for each person)."""
        resources = payload if isinstance(payload, list) and all(isinstance(r, dict) and r.get("resourceType") != "Bundle"
                                                                 for r in payload) else resources_of(payload)
        if not self._busy.acquire(blocking=False):
            raise StoreError("กำลังรับข้อมูลจาก HIS อีกชุดอยู่ ลองใหม่อีกครั้งในอีกสักครู่")
        started = datetime.now(timezone.utc)
        try:
            report = self._run(resources, dry_run=dry_run, actor=actor, window=window)
        finally:
            self._busy.release()
        report.update(mode=mode, dry_run=dry_run, started_at=started.isoformat(timespec="seconds"))
        report["run_id"] = self._record(report, mode, dry_run, by or (actor or {}).get("name"), started)
        return report

    def _record(self, report: dict, mode: str, dry_run: bool, by: str | None, started: datetime) -> int | None:
        try:
            with self.store.db.connect() as conn:
                return conn.execute(
                    """INSERT INTO his_sync_runs (mode, dry_run, status, stats, problems, started_by, started_at, finished_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, NOW()) RETURNING run_id""",
                    (mode, dry_run, report["status"], json.dumps(report["stats"]),
                     json.dumps(report["problems"][:50], ensure_ascii=False), (by or "")[:200] or None, started),
                ).fetchone()["run_id"]
        except Exception as exc:  # the report itself must still reach the caller
            log.warning("could not record the HIS sync: %s", exc)
            return None

    def _run(self, resources: list[dict], *, dry_run: bool, actor: dict | None, window) -> dict:
        admin = self._admin()
        stats = {k: {"new": 0, "changed": 0, "unchanged": 0, "linked": 0, "skipped": 0, "failed": 0}
                 for k in ("room", "staff", "equipment")}
        stats["roster"] = {"days": 0, "unchanged": 0, "added": 0, "removed": 0, "adopted": 0, "skipped": 0, "failed": 0}
        stats["equipment"]["retired"] = 0
        report = {"stats": stats, "problems": [], "skipped": [], "changes": [], "roster_changes": [], "affected": [],
                  "received": {}, "committed": False}
        for r in resources:
            kind = r.get("resourceType")
            report["received"][kind] = report["received"].get(kind, 0) + 1

        def problem(ref: str, entity: str, message: str) -> None:
            stats[entity]["failed"] += 1
            if len(report["problems"]) < MAX_PROBLEMS:
                report["problems"].append({"resource": ref, "entity": entity, "message": message[:300]})

        def skipped(ref: str, entity: str, reason: str) -> None:
            stats[entity]["skipped"] += 1
            if len(report["skipped"]) < 50:
                report["skipped"].append({"resource": ref, "entity": entity, "reason": reason})

        by_type: dict[str, list[dict]] = {t: [] for t in RESOURCE_TYPES}
        for r in resources:
            if r.get("resourceType") in by_type:
                by_type[r["resourceType"]].append(r)
        roles_of: dict[str, list[dict]] = {}
        practitioner_of_role: dict[str, str] = {}
        for pr in by_type["PractitionerRole"]:
            who = _ref(pr.get("practitioner"))
            if who:
                roles_of.setdefault(who, []).append(pr)
                practitioner_of_role[_key(pr)] = who

        class Undo(Exception):
            pass

        try:
            with admin.store.batch() as conn:
                if not self.ready(conn):
                    raise StoreError(NEED_SQL)
                settings = self._settings(conn)
                admin._batch, admin._schema, admin._his = [], None, SOURCE
                known = self._refs(conn)
                saved_refs: list[tuple[str, str, str, str | None]] = []  # (entity, external id, local id, label)

                # ---- rooms (and the names of other locations, for where a device is)
                place: dict[str, str] = {}
                rooms_now = {r["code"].lower() for r in current(admin, "rooms")}
                rows, refs = [], {}
                for r in by_type["Location"]:
                    ref = _key(r)
                    try:
                        room = map_location(r, settings)
                    except Skip as why:
                        label = location_label(r)
                        if label:
                            place[ref] = label[:100]
                            saved_refs.append(("location", ref, label[:100], label[:200]))
                        skipped(ref, "room", str(why))
                        continue
                    except StoreError as exc:
                        problem(ref, "room", str(exc))
                        continue
                    place[ref] = room["code"]
                    new = room["code"].lower() not in rooms_now
                    refs[len(rows) + 1] = (ref, room["code"])
                    rows.append((len(rows) + 1, [room["code"], room["status"], room["type"] if new else None]))
                done = self._table(admin, ROOMS, ("code", "status", "type"), rows, refs, "room", stats, report, problem,
                                   actor, saved_refs)
                stats["room"]["linked"] = self._link(conn, "room", done)

                # ---- staff
                staff_now = {p["code"].lower(): p for p in current(admin, "staff")}
                rows, refs, people = [], {}, {}
                for r in by_type["Practitioner"]:
                    ref = _key(r)
                    try:
                        person = map_practitioner(r, roles_of.get(ref, []), settings)
                    except Skip as why:
                        skipped(ref, "staff", str(why))
                        continue
                    except StoreError as exc:
                        problem(ref, "staff", str(exc))
                        continue
                    stored = staff_now.get(person["code"].lower())
                    people[ref] = person["code"]
                    refs[len(rows) + 1] = (ref, person["code"])
                    rows.append((len(rows) + 1, [person["code"], person["name"], person["role"], person["active"],
                                                 stored["specialty"] if stored else person["specialty"]]))
                done = self._table(admin, STAFF, ("code", "name", "role", "active", "specialty"), rows, refs, "staff",
                                   stats, report, problem, actor, saved_refs)
                stats["staff"]["linked"] = self._link(conn, "staff", done)
                code_to_id = {r["employee_code"].lower(): str(r["staff_id"])
                              for r in conn.execute("SELECT staff_id, employee_code FROM staff")}
                for i, (entity, ref, code, label) in enumerate(saved_refs):
                    if entity == "staff":
                        saved_refs[i] = (entity, ref, code_to_id.get(code.lower(), ""), label)
                for role_ref, who in practitioner_of_role.items():
                    if who in people and people[who].lower() in code_to_id:
                        saved_refs.append(("staff", role_ref, code_to_id[people[who].lower()], people[who]))

                # ---- equipment
                type_names = {t["name"].lower(): t["name"] for t in current(admin, "types")}
                units_now = {u["serial"].lower(): u for u in current(admin, "units")}
                rows, refs, retire = [], {}, {}
                for r in by_type["Device"]:
                    ref = _key(r)
                    try:
                        unit = map_device(r, settings, type_names)
                    except Skip as why:
                        skipped(ref, "equipment", str(why))
                        continue
                    except StoreError as exc:
                        problem(ref, "equipment", str(exc))
                        continue
                    stored = units_now.get(unit["serial"].lower())
                    where = place.get(unit["location"]) or known.get(("room", unit["location"])) \
                        or known.get(("location", unit["location"])) or unit["location_text"]
                    refs[len(rows) + 1] = (ref, unit["serial"])
                    retire[unit["serial"].lower()] = unit["retired"]
                    rows.append((len(rows) + 1, [unit["serial"], unit["type"],
                                                 stored["location"] if stored else (where or None)]))
                done = self._table(admin, UNITS, ("serial", "type", "location"), rows, refs, "equipment", stats,
                                   report, problem, actor, saved_refs)
                stats["equipment"]["linked"] = self._link(conn, "equipment", done)
                stats["equipment"]["retired"] = self._retire(conn, {s: v for s, v in retire.items() if s in done})

                # ---- rosters
                self._rosters(admin, conn, by_type, people, practitioner_of_role, known, code_to_id, settings,
                              window, stats, report, problem, skipped)

                saved = any(stats[k]["new"] or stats[k]["changed"] or stats[k]["linked"]
                            for k in ("room", "staff", "equipment")) or stats["roster"]["days"] or stats["equipment"]["retired"]
                if dry_run or not saved:
                    if not dry_run:  # nothing changed: still remember which resource is which record
                        self._save_refs(conn, saved_refs)
                        admin._batch = None
                        return self._finish(report, dry_run)
                    raise Undo
                self._save_refs(conn, saved_refs)
                admin._batch = None
                admin._his = None
                lines = [f"{ENTITY_TH[k]}: เพิ่ม {stats[k]['new']} แก้ไข {stats[k]['changed']} ผูกกับรายการเดิม "
                         f"{stats[k]['linked']} ไม่เปลี่ยน {stats[k]['unchanged']}"
                         for k in ("room", "staff", "equipment") if any(stats[k][x] for x in ("new", "changed", "unchanged"))]
                if stats["roster"]["days"] or stats["roster"]["unchanged"]:
                    lines.append(f"ตารางเวร: {stats['roster']['days']} วันที่เปลี่ยน (เวรใหม่ {stats['roster']['added']} "
                                 f"เวรที่เอาออก {stats['roster']['removed']})")
                if stats["equipment"]["retired"]:
                    lines.append(f"เครื่องมือปลดระวาง/กลับมาใช้: {stats['equipment']['retired']}")
                admin._log(conn, "รับข้อมูลหลักจาก HIS: " + ", ".join(
                    f"{ENTITY_TH[k]} {stats[k]['new'] + stats[k]['changed']}" for k in ("room", "staff", "equipment")
                    if stats[k]["new"] + stats[k]["changed"]) + (f", เวร {stats['roster']['days']} วัน"
                                                                  if stats["roster"]["days"] else ""), lines, actor)
                today = admin.db.today()
                report["affected"] = admin._affected(conn, today, today + timedelta(days=14))
                report["committed"] = True
        except Undo:
            pass
        finally:
            admin._batch, admin._schema, admin._his = None, None, None
        return self._finish(report, dry_run)

    @staticmethod
    def _finish(report: dict, dry_run: bool) -> dict:
        stats = report["stats"]
        failed = sum(s["failed"] for s in stats.values())
        saved = sum(stats[k]["new"] + stats[k]["changed"] + stats[k]["linked"] for k in ("room", "staff", "equipment")) \
            + stats["roster"]["days"] + stats["equipment"]["retired"]
        report["status"] = "success" if not failed else ("partial" if saved or dry_run else "failed")
        report["problems_total"] = failed
        return report

    def _table(self, admin, sheet, keys, rows, refs, entity, stats, report, problem, actor, saved_refs) -> set:
        """Rows through the Excel import's checks and saves; returns the keys (lower case) that are in."""
        if not rows:
            return set()
        cols = {c.key: c for c in sheet.cols}
        table = Table(sheet, f"FHIR {entity}", [cols[k] for k in keys], rows, [])
        result = _Run(admin, table, actor, "HIS").run()
        failed = {e["row"] for e in result["errors"]}
        for e in result["errors"]:
            problem(refs.get(e["row"], ("?",))[0], entity, f"{e['key']}: {e['message']}" if e["key"] else e["message"])
        for kind in ("new", "changed", "unchanged"):
            stats[entity][kind] += result[kind]
        for row in result["rows"][:MAX_PROBLEMS]:
            report["changes"].append({"entity": entity, "key": row["key"], "action": row["action"],
                                      "changes": row["changes"]})
        ok = set()
        for number, (ref, code) in refs.items():
            if number in failed:
                continue
            ok.add(code.lower())
            saved_refs.append((entity, ref, code, code))
        return ok

    @staticmethod
    def _link(conn, entity: str, keys: set) -> int:
        """Records the HIS sent that were the app's until now (typed in, or from Excel): the HIS's from now on."""
        if not keys:
            return 0
        table, col = LINK[entity]
        return conn.execute(f"UPDATE {table} SET managed_by = %s WHERE lower({col}) = ANY(%s) "  # noqa: S608 (fixed names)
                            "AND managed_by IS DISTINCT FROM %s", (SOURCE, sorted(keys), SOURCE)).rowcount

    def _retire(self, conn, retire: dict[str, bool]) -> int:
        """Units the HIS retired (or brought back): RETIRED units are left out of every plan."""
        changed = 0
        for serial, retired in retire.items():
            if retired:
                cur = conn.execute("""UPDATE equipment_units SET current_status = 'RETIRED'
                                      WHERE lower(serial_number) = %s AND current_status IS DISTINCT FROM 'RETIRED'""",
                                   (serial,))
            else:
                cur = conn.execute("""UPDATE equipment_units SET current_status = 'AVAILABLE', available_at = NOW()
                                      WHERE lower(serial_number) = %s AND current_status = 'RETIRED'""", (serial,))
            changed += cur.rowcount
        return changed

    @staticmethod
    def _refs(conn) -> dict:
        return {(r["entity_type"], r["external_id"]): r["local_id"]
                for r in conn.execute("SELECT entity_type, external_id, local_id FROM his_external_refs")}

    @staticmethod
    def _save_refs(conn, refs: list) -> None:
        rows = [(e, ref[:200], str(local)[:100], (label or "")[:200] or None) for e, ref, local, label in refs if local]
        if not rows:
            return
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO his_external_refs (entity_type, external_id, local_id, label) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (entity_type, external_id)
                   DO UPDATE SET local_id = EXCLUDED.local_id, label = EXCLUDED.label, synced_at = NOW()""", rows)

    # --------------------------------------------------------- rosters

    def _rosters(self, admin, conn, by_type, people, practitioner_of_role, known, code_to_id, settings, window,
                 stats, report, problem, skipped) -> None:
        if not by_type["Schedule"] and not by_type["Slot"]:
            return
        tz = admin.db.tz
        today = admin.db.today()
        names = {str(r["staff_id"]): r["full_name"] for r in conn.execute("SELECT staff_id, full_name FROM staff")}

        def staff_of(actor_ref, identifier) -> str | None:
            who = practitioner_of_role.get(actor_ref, actor_ref)
            if who in people:
                return code_to_id.get(people[who].lower())
            hit = known.get(("staff", actor_ref)) or known.get(("staff", who))
            if hit:
                return hit
            if identifier:
                return code_to_id.get(identifier.lower())
            return None

        schedules: dict[str, tuple[str | None, set[date]]] = {}
        for s in by_type["Schedule"]:
            ref = _key(s)
            sid = None
            for a in s.get("actor") or []:
                target = _ref(a)
                if target and target.split("/")[0] in ("Practitioner", "PractitionerRole"):
                    ident = _text(((a.get("identifier") or {}) if isinstance(a, dict) else {}).get("value"))
                    sid = staff_of(target, ident)
                    if sid or target in people or target in practitioner_of_role:
                        break
                elif isinstance(a, dict) and isinstance(a.get("identifier"), dict) and not target:
                    sid = staff_of(None, _text(a["identifier"].get("value")))
            if sid is None:
                skipped(ref, "roster", "ไม่พบบุคลากรของตารางนี้ (ไม่ใช่บุคลากรห้องผ่าตัด หรือยังไม่ได้ส่ง Practitioner)")
                schedules[ref] = (None, set())
                continue
            days: set[date] = set()
            horizon = s.get("planningHorizon") or {}
            h0, h1 = _when(horizon.get("start"), tz), _when(horizon.get("end"), tz)
            if window:
                h0 = max(h0, datetime.combine(window[0], time(), tzinfo=tz)) if h0 else datetime.combine(window[0], time(), tzinfo=tz)
                h1 = min(h1, datetime.combine(window[1], time(), tzinfo=tz)) if h1 else datetime.combine(window[1], time(), tzinfo=tz)
            if h0 and h1 and h1 > h0:
                d0 = h0.astimezone(tz).date()
                d1 = (h1 - timedelta(microseconds=1)).astimezone(tz).date()
                if (d1 - d0).days >= ROSTER_MAX_DAYS:
                    problem(ref, "roster", f"planningHorizon ยาวเกิน {ROSTER_MAX_DAYS} วัน กรุณาแบ่งส่ง")
                    schedules[ref] = (None, set())
                    continue
                days = {d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)}
            schedules[ref] = (sid, days)

        wanted: dict[tuple[str, date], set] = {}
        for sid, days in schedules.values():
            for day in days if sid else ():
                wanted.setdefault((sid, day), set())
        for slot in by_type["Slot"]:
            ref = _key(slot)
            sref = _ref(slot.get("schedule"))
            sid = schedules.get(sref, (None, None))[0] if sref else None
            if sref not in schedules:
                skipped(ref, "roster", "ไม่พบ Schedule ของช่วงเวลานี้ในข้อมูลที่ส่งมา")
                continue
            if sid is None:
                continue  # its schedule was skipped already
            try:
                shift = map_slot(slot, settings, tz)
            except StoreError as exc:
                problem(ref, "roster", str(exc))
                continue
            began = _when(slot.get("start"), tz)
            if began is None:
                continue  # entered in error
            day = began.astimezone(tz).date()
            if not today - timedelta(days=ROSTER_PAST_DAYS) <= day <= today + timedelta(days=ROSTER_FUTURE_DAYS):
                problem(ref, "roster", f"วันที่ {day.isoformat()} อยู่นอกช่วงที่รับได้ "
                                       f"({ROSTER_PAST_DAYS} วันย้อนหลังถึง {ROSTER_FUTURE_DAYS} วันข้างหน้า)")
                continue
            bucket = wanted.setdefault((sid, day), set())
            if shift:
                bucket.add(shift)
        if not wanted:
            return
        days_all = {d for _, d in wanted}
        if len(days_all) > 400:
            problem("Bundle", "roster", "ตารางเวรครอบคลุมเกิน 400 วัน กรุณาแบ่งส่ง")
            return
        first, last = min(days_all), max(days_all)
        held: dict[tuple[str, date], list] = {}
        for r in conn.execute(
            """SELECT schedule_id, staff_id, shift_start, shift_end, shift_type, note, managed_by FROM staff_schedules
               WHERE shift_start >= %s AND shift_start < %s AND staff_id::text = ANY(%s)""",
            (admin._at(first, time()), admin._at(last + timedelta(days=1), time()), list({s for s, _ in wanted})),
        ):
            key = (str(r["staff_id"]), r["shift_start"].astimezone(tz).date())
            held.setdefault(key, []).append(r)

        def sig(kind, start, end, note):
            return (kind or "REGULAR", start, end, note if kind == "LEAVE" else None)

        per_person: dict[str, list] = {}
        removals, adoptions, inserts = [], [], []
        for key, desired in sorted(wanted.items(), key=lambda kv: (names.get(kv[0][0], ""), kv[0][1])):
            rows = held.get(key, [])
            mine = {sig(r["shift_type"], r["shift_start"], r["shift_end"], r["note"]): r for r in rows if r["managed_by"]}
            local = {sig(r["shift_type"], r["shift_start"], r["shift_end"], r["note"]): r for r in rows if not r["managed_by"]}
            want = {sig(*s) for s in desired}
            gone = [r for s, r in mine.items() if s not in want]
            adopt = [local[s] for s in want if s not in mine and s in local]
            add = [s for s in want if s not in mine and s not in local]
            if not gone and not adopt and not add:
                stats["roster"]["unchanged"] += 1
                continue
            stats["roster"]["days"] += 1
            stats["roster"]["removed"] += len(gone)
            stats["roster"]["adopted"] += len(adopt)
            stats["roster"]["added"] += len(add)
            removals += [r["schedule_id"] for r in gone]
            adoptions += [r["schedule_id"] for r in adopt]
            inserts += [(key[0], s[1], s[2], s[0], s[3], SOURCE) for s in add]
            show = lambda items: "/".join(sorted(  # noqa: E731
                f"{TYPE_TH.get(k, k)} {a.astimezone(tz).strftime('%H:%M')}-{b.astimezone(tz).strftime('%H:%M')}"
                for k, a, b, _ in items)) or "-"
            per_person.setdefault(key[0], []).append({
                "date": key[1].isoformat(), "before": show(list(mine) + list(local)),
                "after": show(list(want) + [s for s in local if s not in want])})
        with conn.cursor() as cur:
            if removals:
                cur.execute("DELETE FROM staff_schedules WHERE schedule_id = ANY(%s)", (removals,))
            if adoptions:
                cur.execute("UPDATE staff_schedules SET managed_by = %s WHERE schedule_id = ANY(%s)", (SOURCE, adoptions))
            if inserts:
                cur.executemany("""INSERT INTO staff_schedules (staff_id, shift_start, shift_end, shift_type, note, managed_by)
                                   VALUES (%s::uuid, %s, %s, %s, %s, %s)""", inserts)
        report["roster_changes"] = [{"name": names.get(sid, sid), "days": days[:31]}
                                    for sid, days in list(per_person.items())[:200]]

    # --------------------------------------------------------- the HIS's FHIR server

    def _client(self, settings: dict):
        import requests

        base = settings.get("base_url")
        if not base:
            raise StoreError("ยังไม่ได้ใส่ที่อยู่ FHIR server ของ HIS")
        check_base_url(base)
        headers = {"Accept": "application/fhir+json", "User-Agent": "ORchestraAI-FHIR-client/1"}
        env = settings.get("token_env")
        if env:
            if not TOKEN_ENV.match(env):
                raise StoreError("ชื่อตัวแปร token ไม่ถูกต้อง")
            token = os.environ.get(env, "").strip()
            if not token:
                raise StoreError(f"ยังไม่ได้ตั้งค่า {env} ใน Railway → Variables")
            headers["Authorization"] = token if " " in token else f"Bearer {token}"
        session = requests.Session()
        session.headers.update(headers)
        session.max_redirects = 0
        return session, base

    @staticmethod
    def _get(session, url: str) -> dict:
        import requests

        try:
            with session.get(url, timeout=TIMEOUT, allow_redirects=False, stream=True) as res:
                if res.is_redirect or 300 <= res.status_code < 400:
                    raise StoreError(f"FHIR server ตอบให้ไปที่อื่น ({res.status_code}) ระบบไม่ตาม redirect")
                if res.status_code in (401, 403):
                    raise StoreError(f"FHIR server ไม่ยอมรับ token ({res.status_code})")
                if res.status_code >= 400:
                    raise StoreError(f"FHIR server ตอบ {res.status_code}")
                body = b""
                for chunk in res.iter_content(64 * 1024):
                    body += chunk
                    if len(body) > RESPONSE_MAX:
                        raise StoreError("FHIR server ส่งข้อมูลหน้าเดียวใหญ่เกิน 25 MB (ลด _count)")
        except requests.RequestException as exc:
            raise StoreError(f"ติดต่อ FHIR server ไม่ได้ ({type(exc).__name__})") from None
        try:
            return json.loads(body)
        except ValueError:
            raise StoreError("FHIR server ตอบกลับไม่ใช่ JSON") from None

    def test(self) -> dict:
        settings = self.settings()
        session, base = self._client(settings)
        with session:
            meta = self._get(session, f"{base}/metadata")
        if meta.get("resourceType") != "CapabilityStatement":
            raise StoreError("ที่อยู่นี้ไม่ใช่ FHIR server (ไม่ได้ตอบ CapabilityStatement จาก /metadata)")
        types = {r.get("type") for rest in meta.get("rest") or [] for r in rest.get("resource") or []}
        return {"ok": True, "fhir_version": meta.get("fhirVersion"),
                "software": " ".join(filter(None, [(meta.get("software") or {}).get("name"),
                                                   (meta.get("software") or {}).get("version")])) or None,
                "resources": {t: t in types for t in RESOURCE_TYPES}}

    def pull(self, *, dry_run: bool, actor: dict | None = None, by: str | None = None) -> dict:
        """Read what changed from the HIS's FHIR server, then apply it like a push."""
        settings = self.settings()
        session, base = self._client(settings)
        origin = urlsplit(base)[:2]
        admin = self._admin()
        today = admin.db.today()
        window = (today - timedelta(days=1), today + timedelta(days=settings["roster_days"] + 1))
        resources, newest = [], {}
        with session:
            for kind in PULL_ORDER:
                params = {"_count": 200}
                cursor = settings["cursors"].get(kind)
                if kind == "Slot":
                    params["start"] = [f"ge{window[0].isoformat()}", f"lt{window[1].isoformat()}"]
                elif cursor and kind != "Schedule":  # every Schedule: its horizon decides which days are the HIS's
                    params["_lastUpdated"] = f"gt{cursor}"
                url = f"{base}/{kind}?{urlencode(params, doseq=True)}"
                for _ in range(PAGE_LIMIT):
                    page = self._get(session, url)
                    for r in resources_of(page):
                        if r.get("resourceType") == kind:
                            resources.append(r)
                            seen = (r.get("meta") or {}).get("lastUpdated")
                            if seen and seen > newest.get(kind, ""):
                                newest[kind] = seen
                    if len(resources) > MAX_RESOURCES:
                        raise StoreError(f"ข้อมูลจาก HIS เกิน {MAX_RESOURCES:,} รายการ (ลดจำนวนวันของตารางเวร)")
                    nxt = next((link.get("url") for link in page.get("link") or []
                                if isinstance(link, dict) and link.get("relation") == "next"), None)
                    if not nxt:
                        break
                    if urlsplit(nxt)[:2] != origin:
                        raise StoreError("FHIR server ให้ไปหน้าถัดไปที่เซิร์ฟเวอร์อื่น ระบบไม่ตาม")
                    url = nxt
        report = self.run(resources, mode="pull", dry_run=dry_run, actor=actor, by=by, window=window)
        if not dry_run and report["status"] != "failed" and newest:
            with admin.store._transaction(write=True) as conn:
                saved = self._settings(conn)
                saved["cursors"] = {**saved.get("cursors", {}),
                                    **{k: v for k, v in newest.items() if k not in ("Schedule", "Slot")}}
                self._write_settings(conn, saved)
        return report

    # --------------------------------------------------------- for the admin page

    def overview(self) -> dict:
        admin = self._admin()
        with admin.store._transaction(write=False) as conn:
            if not self.ready(conn):
                return {"ready": False, "need_sql": NEED_SQL}
            settings = self._settings(conn)
            counts = conn.execute(
                """SELECT (SELECT count(*) FROM operating_rooms WHERE managed_by IS NOT NULL) AS rooms,
                          (SELECT count(*) FROM staff WHERE managed_by IS NOT NULL) AS staff,
                          (SELECT count(*) FROM equipment_units WHERE managed_by IS NOT NULL) AS equipment,
                          (SELECT count(*) FROM staff_schedules WHERE managed_by IS NOT NULL
                             AND shift_end > NOW()) AS shifts""").fetchone()
            runs = [{"id": r["run_id"], "mode": r["mode"], "dry_run": r["dry_run"], "status": r["status"],
                     "stats": r["stats"], "problems": len(r["problems"] or []), "by": r["started_by"],
                     "at": admin._local(r["started_at"])}
                    for r in conn.execute("SELECT * FROM his_sync_runs ORDER BY started_at DESC LIMIT 20")]
        env = settings.get("token_env")
        return {"ready": True, "settings": settings, "managed": dict(counts), "runs": runs,
                "token_set": bool(env and os.environ.get(env, "").strip()), "scope": SCOPE,
                "defaults": {k: DEFAULTS[k] for k in MAP_VALUES}}

    def run_detail(self, run_id: int) -> dict:
        admin = self._admin()
        with admin.store._transaction(write=False) as conn:
            row = conn.execute("SELECT * FROM his_sync_runs WHERE run_id = %s", (run_id,)).fetchone()
        if row is None:
            raise StoreError("ไม่พบประวัติการรับข้อมูลนี้")
        return {"id": row["run_id"], "mode": row["mode"], "dry_run": row["dry_run"], "status": row["status"],
                "stats": row["stats"], "problems": row["problems"], "by": row["started_by"],
                "at": admin._local(row["started_at"])}


def outcome(report: dict) -> dict:
    """A run as a FHIR OperationOutcome (what the HIS gets back from a push)."""
    stats = report["stats"]
    lines = [f"{k}: new {s['new']}, changed {s['changed']}, unchanged {s['unchanged']}, linked {s['linked']}, "
             f"skipped {s['skipped']}, failed {s['failed']}" for k, s in stats.items() if k != "roster"]
    r = stats["roster"]
    lines.append(f"roster: days changed {r['days']}, unchanged {r['unchanged']}, shifts added {r['added']}, "
                 f"removed {r['removed']}, failed {r['failed']}")
    issues = [{"severity": "information", "code": "informational",
               "diagnostics": ("DRY RUN (nothing saved): " if report["dry_run"] else "") + "; ".join(lines)}]
    issues += [{"severity": "error", "code": "processing", "diagnostics": p["message"], "expression": [p["resource"]]}
               for p in report["problems"][:100]]
    return {"resourceType": "OperationOutcome", "issue": issues, "status": report["status"], "dry_run": report["dry_run"],
            "run_id": report.get("run_id"), "stats": stats, "committed": report["committed"]}


class PullTimer:
    """Pull from the HIS every ``pull_hours`` (when set), claimed in app_settings so a second server or a
    restart does not pull the same period twice."""

    def __init__(self, master: MasterData, *, every_s: float = 600.0) -> None:
        self.master, self.every_s = master, every_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="his-pull", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.every_s):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a background job never stops the server
                log.exception("HIS pull failed")

    def tick(self, now: float | None = None) -> dict | None:
        store = self.master.store
        with store.db.connect() as conn:
            if not conn.execute("SELECT to_regclass('public.his_sync_runs') IS NOT NULL AS ok").fetchone()["ok"]:
                return None
            settings = self.master._settings(conn)
            hours = int(settings.get("pull_hours") or 0)
            if not hours or not settings.get("base_url"):
                return None
            period = int((now or clock.time()) // (hours * 3600))
            claimed = conn.execute("""INSERT INTO app_settings (key, value) VALUES (%s, %s)
                                      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                                      WHERE app_settings.value IS DISTINCT FROM EXCLUDED.value RETURNING key""",
                                   ("his_fhir_pull", str(period))).fetchone()
        if claimed is None:
            return None
        report = self.master.pull(dry_run=False, by="ดึงอัตโนมัติ")
        if report["status"] != "success":
            log.error("HIS pull: %s, %d problems", report["status"], report["problems_total"])
        return report
