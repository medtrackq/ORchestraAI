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
    SOLVER_WORKERS     CP-SAT worker threads (default 2)
    SOLVER_TIME_LIMIT  seconds per solve (default 10)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .auth import (COOKIE, ROLES, SESSION_HOURS, Accounts, AuthError, DbUserStore, MemoryUserStore,
                   ServiceUnavailable, TooManyAttempts, User, can)
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
else:
    store = ScheduleStore(**_solver)
    accounts = Accounts(MemoryUserStore())

app = FastAPI(title="ORchestraAI", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")

PUBLIC = {"/login", "/api/login", "/api/health"}
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
    return {"name": user.display_name, "username": user.username, "role": user.role, "staff_id": user.staff_id}


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
