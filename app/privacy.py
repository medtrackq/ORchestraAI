"""PDPA: who was shown patients' identities and when, and removing them after a set time.

Access log: each time a person with the ``patients`` permission is shown
patients' HN / names (the day's schedule, a printout, an HN lookup) a row goes
into ``patient_access_log`` (migration 013) with the case codes, never the HN
or name. Opening the same day again within ``WINDOW`` showing no new case is
not logged again, so a busy morning does not fill the log with copies.

Retention: an admin sets how many days after a case finished (or was called
off) its HN, name and free text are kept. After that ``apply`` replaces them
(HN "ANON-...", name "(ลบข้อมูลแล้ว)", request text, checklist and
cancellation notes, and the engine's change notices in the activity log that
carry the HN). Times, rooms, procedures and teams stay for the reports and
the duration forecast. A daily job (``RetentionTimer``) runs it; the access
log itself is kept ``access_days`` (default two years).
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import psycopg

from .store import StoreError

log = logging.getLogger("orchestra.privacy")

ACTIONS = {"schedule": "เปิดตารางผ่าตัด", "print": "พิมพ์ใบรายการ / เช็กลิสต์", "lookup": "ค้นหาด้วย HN",
           "backup": "ดาวน์โหลดสำรองข้อมูลทั้งหมด"}
WINDOW = timedelta(minutes=30)
MIN_DAYS = 30  # shorter would remove identities of cases still being followed up
MAX_DAYS = 3650
KEY_PATIENT = "retention_patient_days"
KEY_ACCESS = "retention_access_days"
DEFAULT_ACCESS_DAYS = 730
ANON_NAME = "(ลบข้อมูลแล้ว)"
MEMORY_LIMIT = 2000


def _codes_of(state: dict) -> list[str]:
    """Cases whose identity the state shows (the day's bookings and the waiting list)."""
    codes = [b["case_id"] for b in state.get("schedule", {}).get("bookings", [])]
    codes += [p["code"] for p in state.get("pending", []) if p.get("code")]
    return list(dict.fromkeys(codes))


def _table(conn, name: str) -> bool:
    return conn.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"public.{name}",)).fetchone()["ok"]


def _column(conn, table: str, column: str) -> bool:
    return conn.execute(
        """SELECT EXISTS (SELECT 1 FROM information_schema.columns
                          WHERE table_schema = 'public' AND table_name = %s AND column_name = %s) AS ok""",
        (table, column)).fetchone()["ok"]


# ---------------------------------------------------------------- access log

class AccessLog:
    """Records patient-data access; in demo mode (no database) it keeps them in memory."""

    def __init__(self, store) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._recent: dict[tuple, tuple[datetime, frozenset]] = {}
        self._memory: list[dict] = []

    @property
    def supabase(self) -> bool:
        return getattr(self.store, "mode", "demo") == "supabase"

    def ready(self) -> bool:
        if not self.supabase:
            return True
        try:
            with self.store.db.connect() as conn:
                return _table(conn, "patient_access_log")
        except Exception:
            return False

    def record_state(self, user, state: dict, action: str = "schedule", address: str | None = None) -> bool:
        return self.record(user, action, state.get("date"), _codes_of(state), address)

    def record(self, user, action: str, day: str | None, codes: list[str], address: str | None = None,
               *, patients: int | None = None) -> bool:
        """Log one access; False when skipped (nothing shown, or the same view moments ago).

        A full backup holds every patient: it is logged with their number and no case codes.
        """
        if action not in ACTIONS or (not codes and not patients):
            return False
        now = datetime.now().astimezone()
        key = (user.user_id, action, day)
        shown = frozenset(codes)
        with self._lock:
            last = self._recent.get(key)
            if action == "schedule" and last and now - last[0] < WINDOW and shown <= last[1]:
                return False
            self._recent[key] = (now, shown | (last[1] if last and now - last[0] < WINDOW else frozenset()))
            if len(self._recent) > 5000:  # forget the oldest views
                for k in sorted(self._recent, key=lambda k: self._recent[k][0])[:1000]:
                    del self._recent[k]
        row = {"accessed_at": now, "user_id": user.user_id, "username": user.username, "role": user.role,
               "action": action, "target_day": day, "case_codes": sorted(shown),
               "patients": patients if patients is not None else len(shown),
               "address": (address or "")[:64] or None}
        if not self.supabase:
            with self._lock:
                self._memory.append(row)
                del self._memory[:-MEMORY_LIMIT]
            return True
        try:
            with self.store.db.connect() as conn:
                if not _table(conn, "patient_access_log"):
                    self._forget(key)  # not recorded: the next view must try again
                    return False
                conn.execute(
                    """INSERT INTO patient_access_log (accessed_at, user_id, username, role, action, target_day,
                                                       case_codes, patients, address)
                       VALUES (%(accessed_at)s, %(user_id)s, %(username)s, %(role)s, %(action)s, %(target_day)s,
                               %(case_codes)s, %(patients)s, %(address)s)""", row)
            return True
        except Exception as exc:  # the schedule must still open; the failure goes to the server log
            log.warning("could not record patient data access: %s", exc)
            self._forget(key)
            return False

    def _forget(self, key: tuple) -> None:
        with self._lock:
            self._recent.pop(key, None)

    def codes_for_hn(self, hn: str) -> list[str]:
        """Case codes of a patient (every urgency letter: a case keeps its id when its urgency changes)."""
        hn = (hn or "").strip()
        if not hn:
            return []
        if not self.supabase:
            result = self.store.result
            ids = [cid for cid, c in result.cases.items() if c.patient_hn == hn]
            ids += [cid for cid, c in getattr(self.store, "pending", {}).items() if c.patient_hn == hn]
            return ids
        with self.store._transaction(write=False) as conn:
            rows = conn.execute("SELECT request_id FROM surgery_requests WHERE patient_hn = %s", (hn,)).fetchall()
        return [f"{letter}-{r['request_id'].hex[:6].upper()}" for r in rows for letter in "REU"]

    def entries(self, *, username: str | None = None, date_from: str | None = None, date_to: str | None = None,
                case: str | None = None, hn: str | None = None, user=None, address: str | None = None,
                limit: int = 300) -> dict:
        """Newest first. Looking up by HN is itself recorded (it shows which cases are that patient's)."""
        tz = getattr(getattr(self.store, "db", None), "tz", None) or ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Bangkok"))
        today = datetime.now(tz).date()
        try:
            first = date.fromisoformat(date_from) if date_from else today - timedelta(days=30)
            last = date.fromisoformat(date_to) if date_to else today
        except ValueError as exc:
            raise StoreError("วันที่ไม่ถูกต้อง (ใช้ YYYY-MM-DD)") from exc
        if last < first:
            raise StoreError("วันที่สิ้นสุดต้องไม่ก่อนวันที่เริ่ม")
        codes = None
        if case:
            codes = [case.strip().upper()]
        if hn:
            codes = self.codes_for_hn(hn)
            if user is not None:
                self.record(user, "lookup", None, codes, address)
            if not codes:
                return {"entries": [], "ready": self.ready(), "hn_found": False, "actions": ACTIONS}
        start = datetime(first.year, first.month, first.day, tzinfo=tz).astimezone()
        end = datetime(last.year, last.month, last.day, tzinfo=tz).astimezone() + timedelta(days=1)
        username = (username or "").strip().lower() or None
        if not self.supabase:
            with self._lock:
                rows = [r for r in reversed(self._memory)
                        if start <= r["accessed_at"] < end and (username is None or r["username"] == username)
                        and (codes is None or set(codes) & set(r["case_codes"]))][:limit]
            ready = True
        else:
            with self.store._transaction(write=False) as conn:
                ready = _table(conn, "patient_access_log")
                rows = conn.execute(
                    """SELECT accessed_at, user_id, username, role, action, target_day, case_codes, patients, address
                       FROM patient_access_log
                       WHERE accessed_at >= %s AND accessed_at < %s
                         AND (%s::text IS NULL OR username = %s)
                         AND (%s::text[] IS NULL OR case_codes && %s::text[])
                       ORDER BY accessed_at DESC LIMIT %s""",
                    (start, end, username, username, codes, codes, limit)).fetchall() if ready else []
        out = [{**r, "accessed_at": r["accessed_at"].astimezone(tz).isoformat(timespec="seconds"),
                "target_day": r["target_day"].isoformat() if isinstance(r["target_day"], date) else r["target_day"],
                "case_codes": list(r["case_codes"]), "action_label": ACTIONS.get(r["action"], r["action"])}
               for r in rows]
        return {"entries": out, "ready": ready, "hn_found": None if not hn else True, "actions": ACTIONS,
                "from": first.isoformat(), "to": last.isoformat(), "limit": limit}


# ---------------------------------------------------------------- retention

def _setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_settings WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def _days(value, *, allow_none: bool) -> int | None:
    if value in (None, "") and allow_none:
        return None
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise StoreError("จำนวนวันต้องเป็นตัวเลข") from exc
    if not MIN_DAYS <= days <= MAX_DAYS:
        raise StoreError(f"จำนวนวันต้องอยู่ระหว่าง {MIN_DAYS} ถึง {MAX_DAYS} วัน")
    return days


def _require(store):
    if getattr(store, "mode", "demo") != "supabase":
        raise StoreError("ใช้ได้เมื่อเชื่อมฐานข้อมูล Supabase แล้วเท่านั้น (โหมดสาธิตไม่เก็บข้อมูลถาวร)")


def _eligible(conn, days: int) -> list[uuid.UUID]:
    """Requests whose case finished (or was called off) more than ``days`` ago and still carry identity."""
    from .hospital_io import _demo_where

    demo, args = _demo_where()
    called_off = ("(SELECT max(c.cancelled_at) FROM case_cancellations c WHERE c.request_id = r.request_id)"
                  if _table(conn, "case_cancellations") else "NULL")
    rows = conn.execute(
        f"""SELECT r.request_id FROM surgery_requests r
            WHERE r.anonymized_at IS NULL AND r.status IS DISTINCT FROM 'PENDING' AND NOT ({demo})
              AND GREATEST(r.created_at,
                           (SELECT max(s.end_time) FROM scheduled_surgeries s WHERE s.request_id = r.request_id),
                           {called_off}) < now() - make_interval(days => %s)""",
        (*args, days)).fetchall()
    return [r["request_id"] for r in rows]


def retention(store) -> dict:
    """Current settings and how many cases are past the period now."""
    if getattr(store, "mode", "demo") != "supabase":
        return {"ready": False, "supabase": False, "patient_days": None, "access_days": DEFAULT_ACCESS_DAYS,
                "due": 0, "anonymized": 0, "last_run": None, "min_days": MIN_DAYS, "max_days": MAX_DAYS}
    with store._transaction(write=False) as conn:
        ready = _table(conn, "app_settings") and _column(conn, "surgery_requests", "anonymized_at")
        patient_days = access_days = last_run = None
        due = anonymized = 0
        if ready:
            patient_days = _days(_setting(conn, KEY_PATIENT), allow_none=True)
            access_days = _days(_setting(conn, KEY_ACCESS) or DEFAULT_ACCESS_DAYS, allow_none=False)
            last_run = _setting(conn, "retention_last_run")
            due = len(_eligible(conn, patient_days)) if patient_days else 0
            anonymized = conn.execute("SELECT count(*) AS n FROM surgery_requests WHERE anonymized_at IS NOT NULL"
                                      ).fetchone()["n"]
    return {"ready": ready, "supabase": True, "patient_days": patient_days, "access_days": access_days or DEFAULT_ACCESS_DAYS,
            "due": due, "anonymized": anonymized, "last_run": last_run, "min_days": MIN_DAYS, "max_days": MAX_DAYS}


def set_retention(store, patient_days, access_days, *, actor: dict | None = None) -> dict:
    _require(store)
    patient_days = _days(patient_days, allow_none=True)
    access_days = _days(access_days if access_days not in (None, "") else DEFAULT_ACCESS_DAYS, allow_none=False)
    with store._transaction(write=True) as conn:
        if not (_table(conn, "app_settings") and _column(conn, "surgery_requests", "anonymized_at")):
            raise StoreError("ยังตั้งไม่ได้: ให้รัน SQL ไฟล์ 013 (หน้า ⚙ สถานะระบบ → รัน SQL ที่ยังขาด) ก่อน")
        for key, value in ((KEY_PATIENT, patient_days), (KEY_ACCESS, access_days)):
            if value is None:
                conn.execute("DELETE FROM app_settings WHERE key = %s", (key,))
            else:
                conn.execute("""INSERT INTO app_settings (key, value) VALUES (%s, %s)
                                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (key, str(value)))
        title = (f"ตั้งระยะเวลาเก็บข้อมูลผู้ป่วย {patient_days} วันหลังผ่าตัด" if patient_days
                 else "ปิดการลบข้อมูลผู้ป่วยอัตโนมัติ") + f" · เก็บบันทึกการเข้าถึง {access_days} วัน"
        _log(store, conn, title, [], actor)
    return retention(store)


def apply(store, *, actor: dict | None = None) -> dict:
    """Remove identity from cases past the period and prune the access log (both per the saved settings)."""
    _require(store)
    with store._transaction(write=True) as conn:
        if not (_table(conn, "app_settings") and _column(conn, "surgery_requests", "anonymized_at")):
            raise StoreError("ยังใช้ไม่ได้: ให้รัน SQL ไฟล์ 013 ก่อน")
        patient_days = _days(_setting(conn, KEY_PATIENT), allow_none=True)
        access_days = _days(_setting(conn, KEY_ACCESS) or DEFAULT_ACCESS_DAYS, allow_none=False)
        ids = _eligible(conn, patient_days) if patient_days else []
        cleared = scrubbed = pruned = 0
        if ids:
            hns = [r["patient_hn"] for r in conn.execute(
                "SELECT DISTINCT patient_hn FROM surgery_requests WHERE request_id = ANY(%s)", (ids,))]
            conn.execute(
                """UPDATE surgery_requests
                   SET patient_hn = 'ANON-' || upper(left(replace(request_id::text, '-', ''), 8)),
                       patient_name = %s, raw_nlp_input = NULL, anonymized_at = now()
                   WHERE request_id = ANY(%s)""", (ANON_NAME, ids))
            if _table(conn, "case_readiness"):
                conn.execute("UPDATE case_readiness SET note = NULL WHERE request_id = ANY(%s)", (ids,))
            if _table(conn, "case_cancellations"):
                conn.execute("UPDATE case_cancellations SET note = NULL WHERE request_id = ANY(%s)", (ids,))
            if _table(conn, "orchestra_activity"):
                # The engine's change notices name the HN "(HN ...)", also on days after the case.
                for hn in hns:
                    scrubbed += conn.execute(
                        """UPDATE orchestra_activity
                           SET notifications = replace(notifications::text, %s, '(HN ลบแล้ว)')::jsonb
                           WHERE strpos(notifications::text, %s) > 0""", (f"(HN {hn})", f"(HN {hn})")).rowcount
        if patient_days and _table(conn, "orchestra_activity"):
            # Old days: change notices (they name HNs) and cancellation notes (free text) go; the
            # schedule changes themselves stay.
            cleared = conn.execute(
                """UPDATE orchestra_activity
                   SET notifications = '[]'::jsonb,
                       agent_log = CASE WHEN kind = 'cancel' THEN '[]'::jsonb ELSE agent_log END
                   WHERE planning_date < (now() - make_interval(days => %s))::date
                     AND (notifications <> '[]'::jsonb OR (kind = 'cancel' AND agent_log <> '[]'::jsonb))""",
                (patient_days,)).rowcount
        if _table(conn, "patient_access_log"):
            pruned = conn.execute("DELETE FROM patient_access_log WHERE accessed_at < now() - make_interval(days => %s)",
                                  (access_days,)).rowcount
        conn.execute("""INSERT INTO app_settings (key, value) VALUES ('retention_last_run', %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                     (datetime.now(store.db.tz).isoformat(timespec="minutes"),))
        if ids or pruned:
            _log(store, conn, f"ลบข้อมูลระบุตัวผู้ป่วยตามระยะเวลาเก็บ: {len(ids)} เคส",
                 [f"เคสที่เสร็จหรือยกเลิกเกิน {patient_days} วัน: {len(ids)} เคส" if patient_days else "ไม่ได้ตั้งระยะเวลา",
                  f"ล้างข้อความแจ้งเปลี่ยนแปลงในบันทึกกิจกรรม {cleared} วัน",
                  f"ลบบันทึกการเข้าถึงที่เก่ากว่า {access_days} วัน {pruned} รายการ"], actor)
    return {"cases": len(ids), "activity_cleared": cleared, "notices_scrubbed": scrubbed, "access_pruned": pruned,
            "patient_days": patient_days, "access_days": access_days}


def _log(store, conn, title: str, lines: list[str], actor: dict | None) -> None:
    if not _table(conn, "orchestra_activity"):
        return
    world = SimpleNamespace(day=store.db.today(), has_activity=True,
                            has_actor_col=_column(conn, "orchestra_activity", "actor"))
    actor_text = "ระบบ (อัตโนมัติ)" if actor is None else f"{actor.get('name')} ({actor.get('role')})"
    store.db.log_activity(conn, world, {"kind": "admin", "title": title, "log": lines, "changes": [],
                                        "notifications": [], "actor": actor_text})


class RetentionTimer:
    """Once a day, from ``hour`` local time, apply the retention settings (claimed per day in app_settings)."""

    def __init__(self, store, *, hour: int = 2, every_s: float = 900.0) -> None:
        self.store, self.hour, self.every_s = store, hour, every_s
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="retention", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        wait = 60.0
        while not self._stop.wait(wait):
            self.tick()
            wait = self.every_s

    def tick(self, now: datetime | None = None) -> dict | None:
        try:
            if getattr(self.store, "mode", "demo") != "supabase":
                return None
            now = now or datetime.now(self.store.db.tz)
            if now.hour < self.hour:
                return None
            with self.store._transaction(write=False) as conn:
                if not (_table(conn, "app_settings") and _column(conn, "surgery_requests", "anonymized_at")):
                    return None
                claimed = conn.execute(
                    "INSERT INTO app_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING RETURNING key",
                    (f"retention_run:{now.date().isoformat()}", now.isoformat(timespec="minutes"))).fetchone()
            if claimed is None:
                return None
            return apply(self.store)
        except (StoreError, psycopg.Error, OSError):
            log.exception("retention run failed")
            return None
        except Exception:  # never take the server down
            log.exception("retention run failed")
            return None
