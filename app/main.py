"""ORchestraAI web app: API + web UI in one service (deployed on Render).

Run locally:
    APP_PASSWORD=changeme uvicorn app.main:app --reload

Environment:
    DATABASE_URL       Supabase Postgres connection string (Session pooler). Without it
                       the app runs in demo mode on synthetic in-memory data.
    APP_TIMEZONE       planning time zone (default Asia/Bangkok)
    APP_USERNAME       owner admin login (default "orchestra")
    APP_PASSWORD       owner admin password; also signs sessions. Without it (and
                       without DATABASE_URL) the app is open for local development.
    SESSION_SECRET     optional explicit key for signing session cookies
    ANTHROPIC_API_KEY  optional: Intake Agent reads free text with Claude (else keyword rules)
    LINE_CHANNEL_ACCESS_TOKEN, LINE_CHANNEL_SECRET, LINE_ADD_FRIEND_URL
                       optional: phone notifications through the hospital's LINE Official Account
                       (webhook URL: https://<site>/api/line/webhook). Browser push needs no setup.
    INTAKE_MODEL       Claude model for intake (default claude-opus-5-5)
    SOLVER_WORKERS     CP-SAT worker threads (default 2)
    SOLVER_TIME_LIMIT  seconds per solve (default 10)
"""
from __future__ import annotations

import base64
import binascii
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import (COOKIE, ROLES, SESSION_HOURS, Accounts, AuthError, DbUserStore, MemoryUserStore,
                   ServiceUnavailable, TooManyAttempts, User, can)
from . import hospital_io, roster_io
from .admin_store import require_supabase
from .intake_service import IntakeService
from .notify import DbNotifyStore, MemoryNotifyStore, Notifier, NotifyUnavailable
from .readiness import DigestTimer, digest_hour
from .store import Forbidden, ScheduleStore, StoreError

ROOT = Path(__file__).resolve().parent
FLOW_PAGE = ROOT.parent / "web" / "orchestra-flow.html"
TIME = r"^\d{2}:\d{2}$"

_solver = {
    "workers": int(os.environ.get("SOLVER_WORKERS", "2")),
    "time_limit_s": float(os.environ.get("SOLVER_TIME_LIMIT", "10")),
}
if os.environ.get("DATABASE_URL"):
    from .db_store import DatabaseStore

    store = DatabaseStore(os.environ["DATABASE_URL"], tz=os.environ.get("APP_TIMEZONE", "Asia/Bangkok"), **_solver)
    accounts = Accounts(DbUserStore(store.db))
    notifier = Notifier(accounts, DbNotifyStore(store.db))
else:
    store = ScheduleStore(**_solver)
    accounts = Accounts(MemoryUserStore())
    notifier = Notifier(accounts, MemoryNotifyStore())
store.listener = notifier.handle  # phone notifications after every saved change

_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Bangkok"))
intake = IntakeService()  # Claude pass when ANTHROPIC_API_KEY is set, keyword rules otherwise


@asynccontextmanager
async def _lifespan(_app):
    """Background job: the daily reminder of the next day's cases that are not ready."""
    hour = digest_hour()
    timer = DigestTimer(store, notifier.handle, hour=hour) if hour is not None else None
    if timer is not None:
        timer.start()
    yield
    if timer is not None:
        timer.stop()


app = FastAPI(title="ORchestraAI", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
              lifespan=_lifespan)

PUBLIC = {"/login", "/api/login", "/api/health", "/api/line/webhook", "/sw.js", "/manifest.webmanifest",
          "/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png", "/icons/badge-96.png"}
ALLOWED_WHILE_TEMPORARY = {"/api/me", "/api/password", "/api/logout"}


def _open_mode() -> bool:
    """Local development only: no owner password and no database."""
    return not os.environ.get("APP_PASSWORD") and not os.environ.get("DATABASE_URL")


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def _address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


@app.middleware("http")
async def sessions(request: Request, call_next):
    path = request.url.path
    if _open_mode():
        user = User("dev", "dev", "โหมดพัฒนา (ไม่มีรหัสผ่าน)", "admin")
    else:
        user = await run_in_threadpool(accounts.user_from_token, request.cookies.get(COOKIE))
    request.state.user = user

    if path not in PUBLIC:
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "กรุณาเข้าสู่ระบบ"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        if user.must_change_password and path.startswith("/api/") and path not in ALLOWED_WHILE_TEMPORARY:
            return JSONResponse({"detail": "กรุณาเปลี่ยนรหัสผ่านชั่วคราวก่อนใช้งาน"}, status_code=403)

    # CSRF: state changes must be JSON from this site (cross-site forms cannot send JSON).
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/"):
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"detail": "ต้องส่งข้อมูลแบบ JSON"}, status_code=415)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "คำขอมาจากเว็บไซต์อื่น"}, status_code=403)

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


def _user(request: Request, permission: str = "view") -> User:
    user: User = request.state.user
    if not can(user.role, permission):
        raise HTTPException(status_code=403, detail="บทบาทของคุณไม่มีสิทธิ์ทำรายการนี้")
    return user


def _actor(user: User) -> dict:
    return {"name": user.display_name, "username": user.username, "role": user.role, "staff_id": user.staff_id,
            "user_id": user.user_id}


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Forbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (StoreError, AuthError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _shape(state: dict, user: User) -> dict:
    """Per-role view: hide patient identity from roles that do not need it and
    show only the signed-in person's own notifications to clinical staff."""
    state = dict(state)
    patients = can(user.role, "patients")
    everyone = can(user.role, "all_notifications")
    schedule = dict(state["schedule"])
    if not patients:
        schedule["bookings"] = [{**b, "patient_hn": "•••", "patient_name": None} for b in schedule["bookings"]]
        state["pending"] = [{**p, "patient_hn": "•••", "patient_name": None} for p in state.get("pending", [])]
        # checklist notes may hold clinical details: these roles see the status only
        hide = lambda r: {**r, "items": {k: {**i, "note": None} for k, i in r["items"].items()}}  # noqa: E731
        state["readiness"] = {cid: hide(r) for cid, r in state.get("readiness", {}).items()}
        if state.get("readiness_next"):
            state["readiness_next"] = {**state["readiness_next"], "cases": [
                {**c, "readiness": hide(c["readiness"])} for c in state["readiness_next"]["cases"]]}
    state["schedule"] = schedule
    activity = []
    for item in state.get("activity", []):
        item = dict(item)
        notes = item.get("notifications", [])
        if not patients:
            notes = []
        elif not everyone:
            notes = [n for n in notes if n.get("staff_id") == user.staff_id]
        item["notifications"] = notes
        activity.append(item)
    state["activity"] = activity
    # Staffing Agent view: schedulers see everyone's fatigue, clinical staff only their own.
    if not can(user.role, "staff_all"):
        own = lambda sid: user.staff_id is not None and sid == user.staff_id  # noqa: E731
        state["staff"] = [p for p in state.get("staff", []) if own(p["staff_id"])]
        state["fatigue"] = [i for i in state.get("fatigue", []) if own(i["staff_id"])]
    state["me"] = user.public()
    state["roles"] = ROLES
    return state


def _state(user: User, day: str | None) -> dict:
    return _shape(_run(store.state, day), user)


# ---------------------------------------------------------------- pages

@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/login", include_in_schema=False)
def login_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "login.html")


@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "admin.html")


@app.get("/sw.js", include_in_schema=False)
def service_worker() -> FileResponse:
    return FileResponse(ROOT / "static" / "sw.js", media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest() -> FileResponse:
    return FileResponse(ROOT / "static" / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/icons/{name}", include_in_schema=False)
def icon(name: str) -> FileResponse:
    if name not in {"icon-192.png", "icon-512.png", "apple-touch-icon.png", "badge-96.png"}:
        raise HTTPException(status_code=404)
    return FileResponse(ROOT / "static" / "icons" / name, media_type="image/png")


@app.get("/reports", include_in_schema=False)
def reports_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "reports.html")


@app.get("/flow", include_in_schema=False)
def flow() -> FileResponse:
    return FileResponse(FLOW_PAGE)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "mode": store.mode}


# ------------------------------------------------------------- accounts

class LoginIn(BaseModel):
    username: str = Field(max_length=80)
    password: str = Field(max_length=200)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=200)
    new_password: str = Field(max_length=200)


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=150)
    role: Literal["admin", "scheduler", "surgeon", "cssd", "staff"]
    staff_id: str | None = None
    password: str = Field(max_length=200)


class UserPatch(BaseModel):
    display_name: str | None = Field(default=None, max_length=150)
    role: Literal["admin", "scheduler", "surgeon", "cssd", "staff"] | None = None
    staff_id: str | None = None
    is_active: bool | None = None


class ResetIn(BaseModel):
    password: str = Field(max_length=200)


def _set_session(response: Response, request: Request, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_HOURS * 3600, httponly=True, samesite="lax",
                        secure=_https(request), path="/")


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    try:
        user, token = accounts.login(body.username, body.password, _address(request))
    except TooManyAttempts as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    _set_session(response, request, token)
    return {"user": user.public()}


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(request: Request) -> dict:
    return {"user": request.state.user.public(), "mode": store.mode}


@app.post("/api/password")
def change_password(body: PasswordIn, request: Request, response: Response) -> dict:
    token = _run(accounts.change_own_password, request.state.user, body.current_password, body.new_password)
    _set_session(response, request, token)
    return {"ok": True}


@app.get("/api/users")
def list_users(request: Request) -> dict:
    _user(request, "users")
    people = _run(store.people)
    names = {p["id"]: p["name"] for p in people}
    users = [{**u.public(), "staff_name": names.get(u.staff_id)} for u in _run(accounts.store.list)]
    return {"users": users, "people": people, "roles": ROLES}


@app.post("/api/users")
def create_user(body: UserIn, request: Request) -> dict:
    _user(request, "users")
    user = _run(accounts.store.create, body.username, body.display_name, body.role, body.staff_id, body.password)
    return {"user": user.public()}


@app.patch("/api/users/{user_id}")
def update_user(user_id: str, body: UserPatch, request: Request) -> dict:
    _user(request, "users")
    user = _run(accounts.store.update, user_id, **body.model_dump(exclude_unset=True))
    return {"user": user.public()}


@app.post("/api/users/{user_id}/reset-password")
def reset_user_password(user_id: str, body: ResetIn, request: Request) -> dict:
    _user(request, "users")
    _run(accounts.store.set_password, user_id, body.password, temporary=True)
    return {"ok": True}


# ------------------------------------------------------------- schedule

class CaseIn(BaseModel):
    procedure: str = Field(min_length=2, max_length=200)
    patient_hn: str = Field(min_length=1, max_length=50)
    surgeon_id: str
    duration_mins: int = Field(ge=15, le=720)
    urgency: Literal["ELECTIVE", "URGENT", "EMERGENCY"] = "ELECTIVE"
    required_capabilities: list[str] = []
    required_equipment_types: list[str] = []
    requires_icu_post_op: bool = False
    infection_risk: Literal["CLEAN", "CONTAMINATED", "DIRTY"] = "CLEAN"
    robotic_certified_scrub: bool = False
    patient_name: str | None = Field(default=None, max_length=150)
    procedure_id: str | None = None
    required_staff_roles: dict[str, int] | None = None
    required_staff_skills: dict[str, list[str]] | None = None
    airborne: bool = False
    pending_id: str | None = Field(default=None, max_length=64)  # rebook a request waiting for a slot
    required_supplies: dict[str, int] = Field(default={}, max_length=30)  # supply code -> quantity
    recovery_mins: int | None = Field(default=None, ge=0, le=10080)  # PACU / ICU stay; None = default


class SlotIn(BaseModel):
    case: CaseIn
    start: str = Field(pattern=TIME)


class EmergencyIn(BaseModel):
    case: CaseIn
    now: str = Field(pattern=TIME)


class OverrunIn(BaseModel):
    case_id: str
    new_duration_mins: int = Field(ge=15, le=1440)
    now: str = Field(pattern=TIME)


class EquipmentIn(BaseModel):
    status: Literal["AVAILABLE", "IN_USE", "STERILIZING", "MAINTENANCE"]
    ready_at: str | None = Field(default=None, pattern=TIME)


def _own_case(user: User, case: CaseIn) -> None:
    if user.role == "surgeon" and case.surgeon_id != user.staff_id:
        raise HTTPException(status_code=403, detail="ศัลยแพทย์จองได้เฉพาะเคสที่ตัวเองเป็นแพทย์ผ่าตัดหลัก")


@app.get("/api/state")
def state(request: Request, date: str | None = None) -> dict:
    """Schedule, resources and activity for one planning day (default: today)."""
    return _state(_user(request), date)


@app.post("/api/slot-check")
def slot_check(body: SlotIn, request: Request, date: str | None = None) -> dict:
    """Ask the agents about a time; nothing is booked."""
    user = _user(request, "book")
    _own_case(user, body.case)
    return _run(store.slot_check, body.case.model_dump(), body.start, commit=False, day=date, actor=_actor(user))


@app.post("/api/bookings")
def book(body: SlotIn, request: Request, date: str | None = None) -> dict:
    """Same check, then lock the slot that every agent accepts."""
    user = _user(request, "book")
    _own_case(user, body.case)
    result = _run(store.slot_check, body.case.model_dump(), body.start, commit=True, day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date)}


@app.post("/api/emergency")
def emergency(body: EmergencyIn, request: Request, date: str | None = None) -> dict:
    user = _user(request, "emergency")
    _own_case(user, body.case)
    result = _run(store.emergency, body.case.model_dump(), body.now, day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date)}


@app.post("/api/overrun")
def overrun(body: OverrunIn, request: Request, date: str | None = None) -> dict:
    user = _user(request, "overrun")
    result = _run(store.overrun, body.case_id, body.new_duration_mins, body.now, day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date)}


@app.get("/api/equipment")
def equipment(request: Request, date: str | None = None) -> dict:
    _user(request)
    return {"units": _run(store.equipment, date)}


@app.patch("/api/equipment/{unit_id}")
def update_equipment(unit_id: str, body: EquipmentIn, request: Request, date: str | None = None) -> dict:
    """Equipment & Inventory input from CSSD: status and when the unit is ready again."""
    user = _user(request, "equipment")
    result = _run(store.update_equipment, unit_id, body.status, body.ready_at, day=date, actor=_actor(user))
    return {"result": result, "units": _run(store.equipment, date), "state": _state(user, date)}


# ----------------------------------------------------------- intake agent

class IntakeIn(BaseModel):
    text: str = Field(min_length=3, max_length=2000)


class PredictIn(BaseModel):
    surgeon_id: str
    procedure_id: str
    complexity: Literal["simple", "standard", "complex"] = "standard"


class ActualIn(BaseModel):
    start: str | None = Field(default=None, pattern=TIME)
    end: str | None = Field(default=None, pattern=TIME)


@app.get("/api/intake/info")
def intake_info(request: Request, date: str | None = None) -> dict:
    _user(request)
    return intake.info(_run(store.intake_context, date))


@app.post("/api/intake/parse")
def intake_parse(body: IntakeIn, request: Request, date: str | None = None) -> dict:
    """Intake & Triage Agent: request text -> form fields, urgency and duration forecast."""
    user = _user(request, "book")
    ctx = _run(store.intake_context, date)
    only = user.staff_id if user.role == "surgeon" else None
    return _run(intake.parse, ctx, body.text, only_surgeon=only)


@app.post("/api/intake/predict")
def intake_predict(body: PredictIn, request: Request, date: str | None = None) -> dict:
    _user(request, "book")
    ctx = _run(store.intake_context, date)
    return {"prediction": _run(intake.predict, ctx, body.surgeon_id, body.procedure_id, body.complexity)}


@app.post("/api/cases/{case_id}/actual")
def record_actual(case_id: str, body: ActualIn, request: Request, date: str | None = None) -> dict:
    """Record when a case really started / finished; the forecast learns from it."""
    user = _user(request, "record_times")
    result = _run(store.record_actual, case_id, body.start, body.end, day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date)}


class ReadinessIn(BaseModel):
    item: str = Field(max_length=20)
    status: Literal["PENDING", "DONE", "NA", "PROBLEM"]
    note: str | None = Field(default=None, max_length=200)


@app.post("/api/cases/{case_id}/readiness")
def update_readiness(case_id: str, body: ReadinessIn, request: Request, date: str | None = None) -> dict:
    """Pre-op checklist: tick, clear or flag one item of a case (surgeons: their own cases)."""
    user = _user(request, "readiness")
    result = _run(store.readiness_update, case_id, body.model_dump(), day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date)}


@app.post("/api/intake/seed-history")
def seed_history(request: Request) -> dict:
    """Supabase mode, admin: invented past cases with actual times (only if none are recorded)."""
    _user(request, "seed")
    if store.mode != "supabase":
        raise HTTPException(status_code=400, detail="ใช้ได้เฉพาะเมื่อเชื่อม Supabase")
    return _run(store.seed_history)


@app.post("/api/reset")
def reset(request: Request) -> dict:
    """Demo mode only: back to the synthetic demo day."""
    user = _user(request, "reset")
    if store.mode != "demo":
        raise HTTPException(status_code=400, detail="ใช้ได้เฉพาะโหมดข้อมูลจำลอง")
    store.reset()
    return {"state": _state(user, None)}


@app.post("/api/seed-demo")
def seed_demo(request: Request) -> dict:
    """Supabase mode only: fill an EMPTY database with the synthetic demo hospital."""
    user = _user(request, "seed")
    if store.mode != "supabase":
        raise HTTPException(status_code=400, detail="ใช้ได้เฉพาะเมื่อเชื่อม Supabase")
    result = _run(store.seed_demo)
    return {"result": result, "state": _state(user, result["date"])}


# ------------------------------------------------------------- hospital data

class RoomIn(BaseModel):
    code: str | None = Field(default=None, max_length=20)
    type: str = Field(default="STANDARD", max_length=50)
    capabilities: list[str] = Field(default=[], max_length=20)
    base_turnover_mins: int = 30
    infectious_turnover_mins: int = 60
    status: str = "AVAILABLE"
    elective_start: str | None = Field(default=None, pattern=TIME)
    elective_end: str | None = Field(default=None, pattern=TIME)


class ClosureIn(BaseModel):
    room: str = Field(max_length=20)
    start_date: str
    start_time: str = Field(default="00:00", pattern=TIME)
    end_date: str
    end_time: str = Field(default="00:00", pattern=TIME)
    reason: str | None = Field(default=None, max_length=200)


class StaffIn(BaseModel):
    code: str = Field(max_length=50)
    name: str = Field(max_length=150)
    role: str
    specialty: str | None = Field(default=None, max_length=100)
    skills: list[str] = Field(default=[], max_length=30)
    max_hours_per_week: float = 48
    max_hours_per_day: float = 10
    min_rest_hours: float = 8
    active: bool = True


class ShiftsIn(BaseModel):
    staff_ids: list[str] = Field(max_length=200)
    date_from: str
    date_to: str
    weekdays: list[int] = [0, 1, 2, 3, 4, 5, 6]
    start: str = Field(default="08:00", pattern=TIME)
    end: str = Field(default="16:00", pattern=TIME)
    type: Literal["REGULAR", "ON_CALL", "LEAVE"] = "REGULAR"
    note: str | None = Field(default=None, max_length=200)


class ShiftDeleteIn(BaseModel):
    ids: list[str] = Field(max_length=2000)


class EquipmentTypeIn(BaseModel):
    name: str = Field(max_length=150)
    category: str = "INSTRUMENT_TRAY"
    requires_sterilization: bool = True
    sterilization_mins: int = 120


class EquipmentUnitIn(BaseModel):
    serial: str | None = Field(default=None, max_length=100)
    type_id: str
    location: str | None = Field(default=None, max_length=100)


def _admin(request: Request, *permissions: str):
    """Signed-in user with any of ``permissions`` and the Supabase-only hospital data service."""
    user: User = request.state.user
    if not any(can(user.role, p) for p in permissions):
        raise HTTPException(status_code=403, detail="บทบาทของคุณไม่มีสิทธิ์ทำรายการนี้")
    return user, _run(require_supabase, store)


@app.get("/api/admin/rooms")
def admin_rooms(request: Request) -> dict:
    _, admin = _admin(request, "hospital_view")
    return _run(admin.rooms)


@app.post("/api/admin/rooms")
def admin_add_room(body: RoomIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_room, body.model_dump(), actor=_actor(user))


@app.patch("/api/admin/rooms/{code}")
def admin_edit_room(code: str, body: RoomIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_room, body.model_dump(), code=code, actor=_actor(user))


@app.post("/api/admin/closures")
def admin_add_closure(body: ClosureIn, request: Request) -> dict:
    """Close a room for a period (repair, deep cleaning); reports days to re-plan."""
    user, admin = _admin(request, "roster")
    return _run(admin.add_closure, body.model_dump(), actor=_actor(user))


@app.delete("/api/admin/closures/{closure_id}")
def admin_delete_closure(closure_id: str, request: Request) -> dict:
    user, admin = _admin(request, "roster")
    return _run(admin.delete_closure, closure_id, actor=_actor(user))


@app.get("/api/admin/staff")
def admin_staff(request: Request) -> dict:
    _, admin = _admin(request, "hospital_view")
    return _run(admin.staff)


@app.post("/api/admin/staff")
def admin_add_staff(body: StaffIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_staff, body.model_dump(), actor=_actor(user))


@app.patch("/api/admin/staff/{staff_id}")
def admin_edit_staff(staff_id: str, body: StaffIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_staff, body.model_dump(), staff_id=staff_id, actor=_actor(user))


@app.get("/api/admin/shifts")
def admin_shifts(request: Request, date_from: str | None = None, date_to: str | None = None,
                 staff_id: str | None = None) -> dict:
    _, admin = _admin(request, "hospital_view")
    return _run(admin.shifts, date_from, date_to, staff_id)


@app.post("/api/admin/shifts")
def admin_add_shifts(body: ShiftsIn, request: Request) -> dict:
    """Shifts or leave for several people and days at once (end before start = past midnight)."""
    user, admin = _admin(request, "roster")
    return _run(admin.add_shifts, body.model_dump(), actor=_actor(user))


@app.post("/api/admin/shifts/delete")
def admin_delete_shifts(body: ShiftDeleteIn, request: Request) -> dict:
    user, admin = _admin(request, "roster")
    return _run(admin.delete_shifts, body.model_dump(), actor=_actor(user))


@app.get("/api/admin/equipment")
def admin_equipment(request: Request) -> dict:
    _, admin = _admin(request, "hospital_view", "equipment")
    return _run(admin.equipment)


@app.post("/api/admin/equipment-types")
def admin_add_type(body: EquipmentTypeIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_type, body.model_dump(), actor=_actor(user))


@app.patch("/api/admin/equipment-types/{type_id}")
def admin_edit_type(type_id: str, body: EquipmentTypeIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_type, body.model_dump(), type_id=type_id, actor=_actor(user))


@app.post("/api/admin/equipment-units")
def admin_add_unit(body: EquipmentUnitIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_unit, body.model_dump(), actor=_actor(user))


@app.patch("/api/admin/equipment-units/{serial}")
def admin_edit_unit(serial: str, body: EquipmentUnitIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_unit, body.model_dump(), serial=serial, actor=_actor(user))


@app.post("/api/replan")
def replan(request: Request, date: str | None = None) -> dict:
    """Re-plan the cases of a day that have not started yet (fixes conflicts after data changes)."""
    user = _user(request, "roster")
    result = _run(store.replan, date, actor=_actor(user))
    return {"result": result, "state": _state(user, date)}


@app.post("/api/pending/{pending_id}/cancel")
def cancel_pending(pending_id: str, request: Request, date: str | None = None) -> dict:
    """Give up a request that was taken off the schedule and is waiting for a new slot."""
    user = _user(request, "book")
    result = _run(store.cancel_pending, pending_id, actor=_actor(user))
    return {"result": result, "state": _state(user, date)}


# ------------------------------------------------------------- supplies and implants

class SupplyItemIn(BaseModel):
    code: str | None = Field(default=None, max_length=50)
    name: str = Field(max_length=200)
    category: Literal["IMPLANT", "CONSUMABLE"] = "CONSUMABLE"
    unit: str = Field(default="ชิ้น", max_length=30)
    on_hand: int = Field(default=0, ge=0)
    reorder_level: int = Field(default=0, ge=0)
    active: bool = True


class StockCountIn(BaseModel):
    on_hand: int = Field(ge=0)
    note: str | None = Field(default=None, max_length=200)


class ReceiveIn(BaseModel):
    quantity: int | None = Field(default=None, ge=1)
    delivery_id: str | None = None
    note: str | None = Field(default=None, max_length=200)


class DeliveryIn(BaseModel):
    quantity: int = Field(ge=1)
    expected_date: str
    note: str | None = Field(default=None, max_length=200)


class ProcedureSuppliesIn(BaseModel):
    supplies: dict[str, int] = Field(default={}, max_length=30)


@app.get("/api/admin/supplies")
def admin_supplies(request: Request) -> dict:
    _, admin = _admin(request, "inventory_view")
    return _run(admin.inventory)


@app.post("/api/admin/supplies")
def admin_add_supply(body: SupplyItemIn, request: Request) -> dict:
    user, admin = _admin(request, "inventory")
    return _run(admin.save_item, body.model_dump(), actor=_actor(user))


@app.patch("/api/admin/supplies/{code}")
def admin_edit_supply(code: str, body: SupplyItemIn, request: Request) -> dict:
    user, admin = _admin(request, "inventory")
    return _run(admin.save_item, body.model_dump(), code=code, actor=_actor(user))


@app.post("/api/admin/supplies/{code}/count")
def admin_count_supply(code: str, body: StockCountIn, request: Request) -> dict:
    """Stock take: set the counted quantity."""
    user, admin = _admin(request, "inventory")
    return _run(admin.count_stock, code, body.model_dump(), actor=_actor(user))


@app.post("/api/admin/supplies/{code}/receive")
def admin_receive_supply(code: str, body: ReceiveIn, request: Request) -> dict:
    user, admin = _admin(request, "inventory")
    return _run(admin.receive, code, body.model_dump(), actor=_actor(user))


@app.post("/api/admin/supplies/{code}/deliveries")
def admin_expect_supply(code: str, body: DeliveryIn, request: Request) -> dict:
    """An order on its way: counted for cases from the expected date."""
    user, admin = _admin(request, "inventory")
    return _run(admin.expect_delivery, code, body.model_dump(), actor=_actor(user))


@app.delete("/api/admin/deliveries/{delivery_id}")
def admin_cancel_delivery(delivery_id: str, request: Request) -> dict:
    user, admin = _admin(request, "inventory")
    return _run(admin.cancel_delivery, delivery_id, actor=_actor(user))


@app.put("/api/admin/procedures/{procedure_id}/supplies")
def admin_procedure_supplies(procedure_id: str, body: ProcedureSuppliesIn, request: Request) -> dict:
    user, admin = _admin(request, "inventory")
    return _run(admin.set_procedure_supplies, procedure_id, body.model_dump(), actor=_actor(user))


# ------------------------------------------------- equipment maintenance, beds, procedures

class MaintenanceIn(BaseModel):
    serial: str = Field(max_length=100)
    start_date: str
    start_time: str = Field(default="00:00", pattern=TIME)
    end_date: str
    end_time: str = Field(default="00:00", pattern=TIME)
    reason: str | None = Field(default=None, max_length=200)


class BedIn(BaseModel):
    code: str | None = Field(default=None, max_length=20)
    type: Literal["PACU", "ICU_SURGICAL"] = "PACU"
    status: Literal["AVAILABLE", "CLOSED"] = "AVAILABLE"
    note: str | None = Field(default=None, max_length=200)


class OccupyIn(BaseModel):
    until_date: str | None = None  # None = free now
    until_time: str | None = Field(default=None, pattern=TIME)
    note: str | None = Field(default=None, max_length=200)


class ProcedureIn(BaseModel):
    name: str = Field(max_length=200)
    code: str | None = Field(default=None, max_length=20)
    duration: int = Field(default=60, ge=15, le=1440)
    caps: list[str] = Field(default=[], max_length=20)
    icu: bool = False
    infection: Literal["CLEAN", "CONTAMINATED", "DIRTY"] = "CLEAN"
    recovery_mins: int | None = Field(default=None, ge=0, le=10080)
    skills: dict[str, list[str]] = Field(default={}, max_length=4)
    blood: bool | None = None  # needs blood reserved (left out: unchanged)

    def data(self) -> dict:
        out = self.model_dump()
        if out["blood"] is None:
            del out["blood"]
        return out


@app.post("/api/admin/maintenance")
def admin_add_maintenance(body: MaintenanceIn, request: Request) -> dict:
    """Planned maintenance of a machine or set; reports bookings that need it then."""
    user, admin = _admin(request, "equipment")
    return _run(admin.add_maintenance, body.model_dump(), actor=_actor(user))


@app.delete("/api/admin/maintenance/{maintenance_id}")
def admin_delete_maintenance(maintenance_id: str, request: Request) -> dict:
    user, admin = _admin(request, "equipment")
    return _run(admin.delete_maintenance, maintenance_id, actor=_actor(user))


@app.get("/api/admin/beds")
def admin_beds(request: Request) -> dict:
    _, admin = _admin(request, "hospital_view")
    return _run(admin.beds)


@app.post("/api/admin/beds")
def admin_add_bed(body: BedIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_bed, body.model_dump(), actor=_actor(user))


@app.patch("/api/admin/beds/{code}")
def admin_edit_bed(code: str, body: BedIn, request: Request) -> dict:
    user, admin = _admin(request, "roster")
    return _run(admin.save_bed, body.model_dump(), code=code, actor=_actor(user))


@app.post("/api/admin/beds/{code}/occupy")
def admin_occupy_bed(code: str, body: OccupyIn, request: Request) -> dict:
    """A patient outside the OR schedule holds the bed until a time (or the bed is free now)."""
    user, admin = _admin(request, "roster")
    return _run(admin.occupy_bed, code, body.model_dump(), actor=_actor(user))


@app.get("/api/admin/procedures")
def admin_procedures(request: Request) -> dict:
    _, admin = _admin(request, "hospital_view")
    return _run(admin.procedures)


@app.post("/api/admin/procedures")
def admin_add_procedure(body: ProcedureIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_procedure, body.data(), actor=_actor(user))


@app.patch("/api/admin/procedures/{procedure_id}")
def admin_edit_procedure(procedure_id: str, body: ProcedureIn, request: Request) -> dict:
    user, admin = _admin(request, "hospital_data")
    return _run(admin.save_procedure, body.data(), procedure_id=procedure_id, actor=_actor(user))


# ------------------------------------------------------------- management reports

@app.get("/api/reports")
def reports(request: Request, date_from: str | None = None, date_to: str | None = None) -> dict:
    """OR utilisation, on-time starts, turnover, booked-duration accuracy, case mix, staff hours, supplies."""
    _user(request, "reports")
    return _run(store.report, date_from, date_to)


@app.get("/api/reports/cases.csv")
def report_csv(request: Request, date_from: str | None = None, date_to: str | None = None) -> PlainTextResponse:
    """The cases behind the report, without patient identifiers (for Excel)."""
    import csv
    import io

    _user(request, "reports")
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["case", "date", "room", "surgeon", "procedure", "urgency", "booked_start", "booked_end",
                     "actual_start", "actual_end", "booked_mins", "actual_mins", "actual_minus_booked", "team_size"])
    for c in _run(store.report_cases, date_from, date_to):
        local = lambda ts: ts.astimezone(_TZ).strftime("%Y-%m-%d %H:%M") if ts else ""  # noqa: E731
        writer.writerow([c.case_id, local(c.start)[:10], c.room, c.surgeon, c.procedure, c.urgency, local(c.start),
                         local(c.end), local(c.actual_start), local(c.actual_end), c.booked_mins,
                         c.actual_mins if c.actual_mins is not None else "",
                         c.actual_mins - c.booked_mins if c.actual_mins is not None else "", len(c.team)])
    name = f"orchestra-cases-{date_from or 'last30'}-{date_to or 'today'}.csv"
    return PlainTextResponse("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ------------------------------------------------------------- phone notifications

class PushSubscriptionIn(BaseModel):
    subscription: dict
    label: str | None = Field(default=None, max_length=200)


class EndpointIn(BaseModel):
    endpoint: str = Field(max_length=2000)


class PrefsIn(BaseModel):
    prefs: dict[str, bool]


def _notify(fn, *args):
    try:
        return fn(*args)
    except NotifyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/notify")
def notify_status(request: Request) -> dict:
    """The signed-in user's notification settings: devices, LINE link, which events, recent deliveries."""
    return _notify(notifier.status, _user(request))


@app.post("/api/notify/subscribe")
def notify_subscribe(body: PushSubscriptionIn, request: Request) -> dict:
    user = _user(request)
    _notify(notifier.subscribe, user, body.subscription, body.label)
    return _notify(notifier.status, user)


@app.post("/api/notify/unsubscribe")
def notify_unsubscribe(body: EndpointIn, request: Request) -> dict:
    user = _user(request)
    _notify(notifier.unsubscribe, user, body.endpoint)
    return _notify(notifier.status, user)


@app.post("/api/notify/prefs")
def notify_prefs(body: PrefsIn, request: Request) -> dict:
    user = _user(request)
    _notify(notifier.set_prefs, user, body.prefs)
    return _notify(notifier.status, user)


@app.post("/api/notify/line-code")
def notify_line_code(request: Request) -> dict:
    """One-time code (15 min) to send to the hospital's LINE Official Account."""
    user = _user(request)
    return {"code": _notify(notifier.link_code, user), "minutes": 15,
            "add_friend_url": notifier.line.add_friend_url or None}


@app.post("/api/notify/line-unlink")
def notify_line_unlink(request: Request) -> dict:
    user = _user(request)
    _notify(notifier.unlink_line, user)
    return _notify(notifier.status, user)


@app.post("/api/notify/test")
def notify_test(request: Request) -> dict:
    user = _user(request)
    statuses = _notify(notifier.test, user)
    return {"sent": statuses.count("sent"), "failed": len(statuses) - statuses.count("sent"),
            "status": _notify(notifier.status, user)}


@app.post("/api/line/webhook", include_in_schema=False)
async def line_webhook(request: Request) -> dict:
    """LINE Official Account webhook: links accounts from one-time codes (signature checked)."""
    body = await request.body()
    try:
        linked = await run_in_threadpool(notifier.line_webhook, body, request.headers.get("x-line-signature"))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail="bad signature") from exc
    except NotifyUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"linked": linked}


# ------------------------------------------------------------- preference cards

class CardEquipmentIn(BaseModel):
    type_id: str
    count: int = Field(default=1, ge=0, le=5)


class CardIn(BaseModel):
    surgeon_id: str | None = None
    procedure_id: str | None = None
    duration: int = Field(ge=15, le=1440)
    stddev: int = Field(default=15, ge=0, le=240)
    roles: dict[str, int] = Field(default={}, max_length=4)
    equipment: list[CardEquipmentIn] = Field(default=[], max_length=20)
    notes: str | None = Field(default=None, max_length=1000)


@app.get("/api/admin/cards")
def admin_cards(request: Request) -> dict:
    """Preference cards with each surgeon's recorded times (surgeons see their own)."""
    user, admin = _admin(request, "cards")
    return _run(admin.cards, _actor(user))


@app.post("/api/admin/cards")
def admin_add_card(body: CardIn, request: Request) -> dict:
    user, admin = _admin(request, "cards")
    return _run(admin.save_card, body.model_dump(), actor=_actor(user))


@app.patch("/api/admin/cards/{card_id}")
def admin_edit_card(card_id: str, body: CardIn, request: Request) -> dict:
    user, admin = _admin(request, "cards")
    return _run(admin.save_card, body.model_dump(), card_id=card_id, actor=_actor(user))


@app.delete("/api/admin/cards/{card_id}")
def admin_delete_card(card_id: str, request: Request) -> dict:
    user, admin = _admin(request, "cards")
    return _run(admin.delete_card, card_id, actor=_actor(user))


# ------------------------------------------------- Excel import / export, demo data

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class ImportIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=7_200_000)  # base64 of a file up to 5 MB
    commit: bool = False  # False: check and show what would change, save nothing


class WipeIn(BaseModel):
    confirm: str = Field(default="", max_length=50)


@app.get("/api/admin/export.xlsx")
def admin_export(request: Request, blank: bool = False) -> Response:
    """The hospital data as an Excel workbook to edit and upload back (``blank``: the empty form)."""
    _, admin = _admin(request, "hospital_data")
    data = _run(hospital_io.build_workbook, admin, blank=blank)
    name = f"orchestra-{'form' if blank else 'hospital-data'}-{admin.db.today().isoformat()}.xlsx"
    return Response(data, media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.post("/api/admin/import")
def admin_import(body: ImportIn, request: Request) -> dict:
    """Check an uploaded workbook (and save it when ``commit`` and it has no errors)."""
    user, admin = _admin(request, "hospital_data")
    text = body.content.split(",", 1)[1] if body.content.startswith("data:") else body.content
    try:
        content = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="อ่านไฟล์ไม่ได้ (ข้อมูลไฟล์ไม่ถูกต้อง)") from exc
    return _run(hospital_io.import_file, admin, body.filename, content, commit=body.commit, actor=_actor(user))


@app.get("/api/admin/demo-data")
def admin_demo_status(request: Request) -> dict:
    _, admin = _admin(request, "seed")
    return _run(hospital_io.data_status, admin)


@app.post("/api/admin/demo-data/delete-cases")
def admin_delete_demo_cases(request: Request) -> dict:
    """Remove the demo day's cases and the invented history; everything else stays."""
    user, admin = _admin(request, "seed")
    return _run(hospital_io.delete_demo_cases, admin, actor=_actor(user))


@app.post("/api/admin/wipe")
def admin_wipe(body: WipeIn, request: Request) -> dict:
    """Delete all hospital data and bookings to start over (user accounts stay)."""
    user, admin = _admin(request, "seed")
    return _run(hospital_io.wipe_hospital, admin, body.confirm, actor=_actor(user))


# ------------------------------------------------------------ roster in Excel

class RosterImportIn(ImportIn):
    month: str | None = Field(default=None, max_length=20)  # for day-number headers when the file does not say


@app.get("/api/admin/roster.xlsx")
def admin_roster_export(request: Request, month: str | None = None, role: str | None = None) -> Response:
    """One month's shifts, on-call and leave as an Excel roster (people x days, shift codes)."""
    _, admin = _admin(request, "roster")
    data = _run(roster_io.build_roster, admin, month, role or None)
    name = f"orchestra-roster-{(month or admin.db.today().strftime('%Y-%m'))[:7]}.xlsx"
    return Response(data, media_type=XLSX,
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.post("/api/admin/roster/import")
def admin_roster_import(body: RosterImportIn, request: Request) -> dict:
    """Check an uploaded roster (and save it when ``commit`` and it has no errors)."""
    user, admin = _admin(request, "roster")
    text = body.content.split(",", 1)[1] if body.content.startswith("data:") else body.content
    try:
        content = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="อ่านไฟล์ไม่ได้ (ข้อมูลไฟล์ไม่ถูกต้อง)") from exc
    return _run(roster_io.import_roster, admin, body.filename, content, commit=body.commit, month=body.month,
                actor=_actor(user))
