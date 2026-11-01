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
import json
import logging
import re
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import psycopg
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auth import (COOKIE, ENV_ADMIN_ID, PENDING_S, ROLES, SESSION_HOURS, Accounts, AuthError, CodeNeeded, DbUserStore,
                   MemoryUserStore, ServiceUnavailable, TooManyAttempts, User, can, env_totp_secret, required_roles)
from . import backup, feedback as fb, his as his_mod, history_io, hospital_io, privacy, roster_io, system_status
from .admin_store import require_supabase
from .intake_service import IntakeService
from .notify import DbNotifyStore, MemoryNotifyStore, Notifier, NotifyUnavailable
from .readiness import DigestTimer, digest_hour
from .store import Forbidden, ScheduleStore, StoreError

ROOT = Path(__file__).resolve().parent
FLOW_PAGE = ROOT.parent / "web" / "orchestra-flow.html"
TIME = r"^\d{2}:\d{2}$"
DATE = r"^\d{4}-\d{2}-\d{2}$"

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
access = privacy.AccessLog(store)  # PDPA: who was shown patients' HN / names
feedback = fb.Feedback(store, notifier.handle)  # problem reports from users and errors, for the admin
his = his_mod.His(store, notifier.handle, access)  # the hospital information system calls in with an API key
logging.getLogger("orchestra").addHandler(fb.ErrorLogHandler(feedback))  # errors the app logs are listed too


@asynccontextmanager
async def _lifespan(_app):
    """Background jobs: the daily reminder of the next day's not-ready cases; the PDPA retention run."""
    hour = digest_hour()
    timers = [DigestTimer(store, notifier.handle, hour=hour)] if hour is not None else []
    if store.mode == "supabase":
        timers.append(privacy.RetentionTimer(store))
        await run_in_threadpool(system_status.record_boot, store)  # frequent starts = the server sleeps when idle
    for timer in timers:
        timer.start()
    yield
    for timer in timers:
        timer.stop()


app = FastAPI(title="ORchestraAI", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json",
              redirect_slashes=False,  # no "/x/" -> "http://.../x" hop (an API key could follow it unencrypted)
              lifespan=_lifespan)

PUBLIC = {"/login", "/help", "/api/login", "/api/login/code", "/api/health", "/api/line/webhook", "/sw.js", "/feedback.js", "/manifest.webmanifest",
          "/icons/icon-192.png", "/icons/icon-512.png", "/icons/apple-touch-icon.png", "/icons/badge-96.png"}
ALLOWED_WHILE_TEMPORARY = {"/api/me", "/api/password", "/api/logout"}
# A role that must use two-step sign-in and has not set it up yet: only setting it up (and leaving).
ALLOWED_WHILE_2FA_SETUP = {"/api/me", "/api/logout", "/api/2fa", "/api/2fa/setup", "/api/2fa/enable",
                           "/api/feedback", "/api/feedback/mine", "/api/client-errors"}
PAGES_WHILE_2FA_SETUP = {"/security", "/help", "/login", "/feedback.js", "/sw.js", "/manifest.webmanifest"}
PENDING_COOKIE = "orchestra_2fa"
HIS_PREFIX = "/api/his/v1/"


def _open_mode() -> bool:
    """Local development only: no owner password and no database."""
    return not os.environ.get("APP_PASSWORD") and not os.environ.get("DATABASE_URL")


def _https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "") == "https"


def _address(request: Request) -> str:
    """The client's IP. Render runs behind Cloudflare, which always overwrites CF-Connecting-IP; the first
    X-Forwarded-For entry is whatever the client sent (Render only appends), so it is the fallback only.
    Login limits do not rely on it alone (there is also a per-user-name cap)."""
    for value in (request.headers.get("cf-connecting-ip", ""),
                  request.headers.get("x-forwarded-for", "").split(",")[0]):
        value = value.strip()
        if value:
            return value[:64]
    return request.client.host if request.client else "unknown"


MAX_BODY = 12 * 1024 * 1024  # an Excel upload (5 MB) as base64 JSON fits; anything bigger is refused unread
# Smaller limits where no file is sent (the HIS: 200 requests fit in well under 1 MB).
BODY_LIMITS = ((HIS_PREFIX, 1024 * 1024), ("/api/feedback", 64 * 1024), ("/api/client-errors", 64 * 1024),
               ("/api/login", 16 * 1024), ("/api/line/webhook", 256 * 1024))
TOO_LARGE = "ข้อมูลที่ส่งมาใหญ่เกินไป"


def _body_limit(path: str) -> int:
    return next((limit for prefix, limit in BODY_LIMITS if path.startswith(prefix)), MAX_BODY)


class BodyLimit:
    """Bodies over the limit for their path are refused: by Content-Length before anything is read, and
    while reading when no length is given (chunked), so no more than the limit is ever read."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = _body_limit(scope["path"])
        length = next((v for k, v in scope["headers"] if k == b"content-length"), None)
        if length is not None:
            try:
                too_big = int(length) > limit
            except ValueError:
                too_big = True
            if too_big:
                return await JSONResponse({"detail": TOO_LARGE}, status_code=413)(scope, receive, send)
            return await self.app(scope, receive, send)
        seen, over, replied = 0, False, False

        async def counted():
            nonlocal seen, over
            if over:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:  # stop reading: to the app the client has gone (nothing is run)
                    over = True
                    return {"type": "http.disconnect"}
            return message

        async def answer(message):
            nonlocal replied
            if not over:
                return await send(message)
            if not replied:  # whatever the app answered to the cut-off body, the caller is told why
                replied = True
                body = json.dumps({"detail": TOO_LARGE}, ensure_ascii=False).encode()
                await send({"type": "http.response.start", "status": 413,
                            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})

        return await self.app(scope, counted, answer)
# The pages use inline scripts and styles and Google Fonts only; nothing may load from elsewhere or frame them.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' "
       "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
       "connect-src 'self'; worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


@app.middleware("http")
async def sessions(request: Request, call_next):
    path = request.scope["path"]  # exactly what the router matches (request.url also folds in the Host header)
    machine = path.startswith(HIS_PREFIX)  # the HIS signs in with its API key in each call, not a session
    if machine:
        request.state.user = None
        denied = await run_in_threadpool(_his_gate, request)  # before the body is read or parsed
        if denied is not None:
            return denied
    elif _open_mode():
        request.state.user = User("dev", "dev", "โหมดพัฒนา (ไม่มีรหัสผ่าน)", "admin")
    else:
        request.state.user = await run_in_threadpool(accounts.user_from_token, request.cookies.get(COOKIE))
    user = request.state.user

    if path not in PUBLIC and not machine:
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "กรุณาเข้าสู่ระบบ"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        if user.must_change_password and path.startswith("/api/") and path not in ALLOWED_WHILE_TEMPORARY:
            return JSONResponse({"detail": "กรุณาเปลี่ยนรหัสผ่านชั่วคราวก่อนใช้งาน"}, status_code=403)
        if user.must_setup_2fa and not user.must_change_password:
            if path.startswith("/api/") and path not in ALLOWED_WHILE_2FA_SETUP:
                return JSONResponse({"detail": "กรุณาตั้งค่าการยืนยันตัวตน 2 ชั้นก่อนใช้งาน (หน้าความปลอดภัยของบัญชี)"},
                                    status_code=403)
            if not path.startswith("/api/") and path not in PAGES_WHILE_2FA_SETUP and not path.startswith("/icons/"):
                return RedirectResponse("/security", status_code=303)

    # CSRF: state changes must be JSON from this site (cross-site forms cannot send JSON).
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/"):
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse({"detail": "ต้องส่งข้อมูลแบบ JSON"}, status_code=415)
        origin = request.headers.get("origin")
        try:
            foreign = bool(origin) and urlparse(origin).netloc != request.headers.get("host")
        except ValueError:  # a malformed Origin is never this site
            foreign = True
        if foreign:
            return JSONResponse({"detail": "คำขอมาจากเว็บไซต์อื่น"}, status_code=403)

    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "microphone=(self), camera=(), geolocation=()"
    if not path.startswith("/api/docs"):  # Swagger UI loads its files from a CDN
        response.headers["Content-Security-Policy"] = CSP
    if _https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


app.add_middleware(BodyLimit)  # added last, so it runs first (before the session lookup)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Which fields are wrong, without sending what was sent back (it may be large, or hold patient data)."""
    errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()[:20]]
    return JSONResponse({"detail": errors}, status_code=422)


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


FREE_TEXT_ACTIVITY = {"cancel", "admin"}  # activity kinds whose log holds text people typed


def _shape(state: dict, user: User) -> dict:
    """Per-role view: hide patient identity from roles that do not need it and
    show only the signed-in person's own notifications to clinical staff."""
    state = dict(state)
    patients = can(user.role, "patients")
    everyone = can(user.role, "all_notifications")
    schedule = dict(state["schedule"])
    if not patients:
        schedule["bookings"] = [{**b, "patient_hn": "•••", "patient_name": None} for b in schedule["bookings"]]
        state["pending"] = [{**p, "patient_hn": "•••", "patient_name": None, "his_ref": None}  # the HIS number finds the patient too
                            for p in state.get("pending", [])]
        # checklist notes may hold clinical details: these roles see the status only
        hide = lambda r: {**r, "items": {k: {**i, "note": None} for k, i in r["items"].items()}}  # noqa: E731
        state["readiness"] = {cid: hide(r) for cid, r in state.get("readiness", {}).items()}
        if state.get("readiness_next"):
            state["readiness_next"] = {**state["readiness_next"], "cases": [
                {**c, "readiness": hide(c["readiness"])} for c in state["readiness_next"]["cases"]]}
    def own_notes(notes: list) -> list:  # change notices name the HN
        if not patients:
            return []
        return notes if everyone else [n for n in notes if n.get("staff_id") == user.staff_id]

    schedule["notifications"] = own_notes(schedule.get("notifications", []))
    state["schedule"] = schedule
    activity = []
    for item in state.get("activity", []):
        item = dict(item)
        item["notifications"] = own_notes(item.get("notifications", []))
        if not patients and item.get("kind") in FREE_TEXT_ACTIVITY:
            item["log"] = []  # cancellation reasons and bed notes are typed by people: may name the patient
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


def _state(user: User, day: str | None, request: Request | None = None, action: str = "schedule") -> dict:
    state = _shape(_run(store.state, day), user)
    if can(user.role, "patients"):  # PDPA: this person is being shown these patients' identities
        access.record_state(user, state, action, _address(request) if request is not None else None)
    state["hospital_name"] = (os.environ.get("HOSPITAL_NAME") or "").strip()[:120] or None  # printed headers
    return state


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


@app.get("/feedback.js", include_in_schema=False)
def feedback_script() -> FileResponse:
    """The report-a-problem button and the page error reporter, shared by the pages."""
    return FileResponse(ROOT / "static" / "feedback.js", media_type="text/javascript")


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


@app.get("/board", include_in_schema=False)
def board_page() -> FileResponse:
    """Today's case status board for a screen in the OR suite or the waiting room (no patient identity)."""
    return FileResponse(ROOT / "static" / "board.html")


@app.get("/print", include_in_schema=False)
def print_page() -> FileResponse:
    """Printable daily OR list (per room) and WHO Surgical Safety Checklist (per case)."""
    return FileResponse(ROOT / "static" / "print.html")


@app.get("/status", include_in_schema=False)
def status_page() -> FileResponse:
    """Admin: which SQL files the database has, the server settings and a go-live checklist."""
    return FileResponse(ROOT / "static" / "status.html")


@app.get("/help", include_in_schema=False)
def help_page() -> FileResponse:
    """User manual per role (Thai), readable before signing in and printable for training."""
    return FileResponse(ROOT / "static" / "help.html")


@app.get("/privacy", include_in_schema=False)
def privacy_page() -> FileResponse:
    """Admin: who was shown patient data, and how long patient identity is kept (PDPA)."""
    return FileResponse(ROOT / "static" / "privacy.html")


@app.get("/security", include_in_schema=False)
def security_page() -> FileResponse:
    """Everyone: two-step sign-in for their own account (set up, recovery codes, new phone)."""
    return FileResponse(ROOT / "static" / "security.html")


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


def _auth_errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except TooManyAttempts as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except ServiceUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response) -> dict:
    try:
        user, token = _auth_errors(accounts.login, body.username, body.password, _address(request))
    except HTTPException as exc:
        if not isinstance(exc.__cause__, CodeNeeded):
            raise
        # Password right; the session comes after the code. Until then only this short-lived cookie.
        response.set_cookie(PENDING_COOKIE, exc.__cause__.pending, max_age=PENDING_S, httponly=True,
                            samesite="strict", secure=_https(request), path="/api/login")
        return {"needs_code": True, "display_name": exc.__cause__.user.display_name}
    _set_session(response, request, token)
    return {"user": user.public()}


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=40)


@app.post("/api/login/code")
def login_code(body: CodeIn, request: Request, response: Response) -> dict:
    """Second step: the 6-digit code from the authenticator app, or a recovery code."""
    user, token, used = _auth_errors(accounts.verify_code, request.cookies.get(PENDING_COOKIE), body.code,
                                     _address(request))
    response.delete_cookie(PENDING_COOKIE, path="/api/login")
    _set_session(response, request, token)
    return {"user": user.public(), **used}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    """Signs out on the server too: the cookie stops working even if someone copied it."""
    accounts.logout(request.cookies.get(COOKIE))
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


# ------------------------------------------------------------- two-step sign-in (own account)

class SetupDoneIn(BaseModel):
    setup_token: str = Field(max_length=600)
    code: str = Field(min_length=6, max_length=40)
    current_code: str | None = Field(default=None, max_length=40)


@app.get("/api/2fa")
def two_factor_status(request: Request) -> dict:
    user: User = request.state.user
    owner = user.user_id == ENV_ADMIN_ID
    ready = owner or (store.mode == "supabase" and _run(accounts.store.two_factor_ready)) or store.mode != "supabase"
    return {"enabled": user.totp_enabled, "required": user.two_factor_required, "must_setup": user.must_setup_2fa,
            "owner_account": owner, "ready": bool(ready), "recovery_left": _run(accounts.recovery_left, user),
            "required_roles": sorted(required_roles()), "demo": store.mode != "supabase"}


@app.post("/api/2fa/setup")
def two_factor_setup(request: Request) -> dict:
    """A new secret as a QR code (nothing changes until the first code from the app is typed)."""
    return _run(accounts.start_setup, request.state.user)


@app.post("/api/2fa/enable")
def two_factor_enable(body: SetupDoneIn, request: Request, response: Response) -> dict:
    out = _auth_errors(accounts.enable, request.state.user, body.setup_token, body.code, body.current_code,
                       _address(request))
    if out.get("token"):  # other devices are signed out; this one carries on
        _set_session(response, request, out.pop("token"))
    return out


@app.post("/api/2fa/disable")
def two_factor_disable(body: CodeIn, request: Request, response: Response) -> dict:
    token = _auth_errors(accounts.disable, request.state.user, body.code, _address(request))
    _set_session(response, request, token)
    return {"ok": True}


@app.post("/api/2fa/recovery")
def two_factor_recovery(body: CodeIn, request: Request) -> dict:
    """New recovery codes (the old ones stop working)."""
    return {"recovery_codes": _auth_errors(accounts.new_recovery_codes, request.state.user, body.code, _address(request))}


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


@app.post("/api/users/{user_id}/reset-2fa")
def reset_user_two_factor(user_id: str, request: Request) -> dict:
    """Someone lost their phone and recovery codes: they set two-step sign-in up again at next sign-in."""
    actor = _user(request, "users")
    return {"user": _run(accounts.reset_two_factor, user_id, actor).public()}


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
    due_date: str | None = Field(default=None, pattern=DATE)  # operate by (waiting list priority)


class SlotIn(BaseModel):
    case: CaseIn
    start: str = Field(pattern=TIME)


class SearchIn(SlotIn):
    days: int = Field(default=14, ge=1, le=28)
    skip_weekends: bool = True


class WaitIn(BaseModel):
    case: CaseIn


class DueIn(BaseModel):
    due_date: str | None = Field(default=None, pattern=DATE)


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
def state(request: Request, date: str | None = None, view: Literal["schedule", "print"] = "schedule") -> dict:
    """Schedule, resources and activity for one planning day (default: today). ``view=print``: the printout."""
    return _state(_user(request), date, request, view)


@app.post("/api/slot-check")
def slot_check(body: SlotIn, request: Request, date: str | None = None) -> dict:
    """Ask the agents about a time; nothing is booked."""
    user = _user(request, "book")
    _own_case(user, body.case)
    return _run(store.slot_check, body.case.model_dump(), body.start, commit=False, day=date, actor=_actor(user))


SEARCHES = threading.BoundedSemaphore(3)  # a search may take ~40 s and holds a connection and a worker thread
_searching: set[str] = set()
_searching_lock = threading.Lock()


@app.post("/api/slot-search")
def slot_search(body: SearchIn, request: Request, date: str | None = None) -> dict:
    """The first free days (up to 3) within ``days`` from ``date``; nothing is booked."""
    user = _user(request, "book")
    _own_case(user, body.case)
    with _searching_lock:  # one search at a time per person
        if user.username in _searching:
            raise HTTPException(status_code=429, detail="กำลังค้นหาวันว่างอยู่ รอผลการค้นหาก่อน")
        _searching.add(user.username)
    try:
        if not SEARCHES.acquire(timeout=10):
            raise HTTPException(status_code=429, detail="มีการค้นหาวันว่างหลายรายการพร้อมกัน กรุณาลองใหม่ในอีกสักครู่")
        try:
            return _run(store.find_slots, body.case.model_dump(), body.start, day=date, days=body.days,
                        skip_weekends=body.skip_weekends, actor=_actor(user))
        finally:
            SEARCHES.release()
    finally:
        with _searching_lock:
            _searching.discard(user.username)


@app.post("/api/waitlist")
def add_waiting(body: WaitIn, request: Request, date: str | None = None) -> dict:
    """Put a request on the waiting list without a day."""
    user = _user(request, "book")
    _own_case(user, body.case)
    result = _run(store.add_waiting, body.case.model_dump(), actor=_actor(user))
    return {"result": result, "state": _state(user, date, request)}


@app.patch("/api/pending/{pending_id}")
def set_due(pending_id: str, body: DueIn, request: Request, date: str | None = None) -> dict:
    """Set or clear the date a waiting request should be operated by."""
    user = _user(request, "book")
    result = _run(store.set_due, pending_id, body.due_date, actor=_actor(user))
    return {"result": result, "state": _state(user, date, request)}


@app.post("/api/bookings")
def book(body: SlotIn, request: Request, date: str | None = None) -> dict:
    """Same check, then lock the slot that every agent accepts."""
    user = _user(request, "book")
    _own_case(user, body.case)
    result = _run(store.slot_check, body.case.model_dump(), body.start, commit=True, day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date, request)}


@app.post("/api/emergency")
def emergency(body: EmergencyIn, request: Request, date: str | None = None) -> dict:
    user = _user(request, "emergency")
    _own_case(user, body.case)
    result = _run(store.emergency, body.case.model_dump(), body.now, day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date, request)}


@app.post("/api/overrun")
def overrun(body: OverrunIn, request: Request, date: str | None = None) -> dict:
    user = _user(request, "overrun")
    result = _run(store.overrun, body.case_id, body.new_duration_mins, body.now, day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date, request)}


@app.get("/api/equipment")
def equipment(request: Request, date: str | None = None) -> dict:
    _user(request)
    return {"units": _run(store.equipment, date)}


@app.patch("/api/equipment/{unit_id}")
def update_equipment(unit_id: str, body: EquipmentIn, request: Request, date: str | None = None) -> dict:
    """Equipment & Inventory input from CSSD: status and when the unit is ready again."""
    user = _user(request, "equipment")
    result = _run(store.update_equipment, unit_id, body.status, body.ready_at, day=date, actor=_actor(user))
    return {"result": result, "units": _run(store.equipment, date), "state": _state(user, date, request)}


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
    return {"result": result, "state": _state(user, date, request)}


class ReadinessIn(BaseModel):
    item: str = Field(max_length=20)
    status: Literal["PENDING", "DONE", "NA", "PROBLEM"]
    note: str | None = Field(default=None, max_length=200)


@app.post("/api/cases/{case_id}/readiness")
def update_readiness(case_id: str, body: ReadinessIn, request: Request, date: str | None = None) -> dict:
    """Pre-op checklist: tick, clear or flag one item of a case (surgeons: their own cases)."""
    user = _user(request, "readiness")
    result = _run(store.readiness_update, case_id, body.model_dump(), day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date, request)}


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
    return {"state": _state(user, None, request)}


@app.post("/api/seed-demo")
def seed_demo(request: Request) -> dict:
    """Supabase mode only: fill an EMPTY database with the synthetic demo hospital."""
    user = _user(request, "seed")
    if store.mode != "supabase":
        raise HTTPException(status_code=400, detail="ใช้ได้เฉพาะเมื่อเชื่อม Supabase")
    result = _run(store.seed_demo)
    return {"result": result, "state": _state(user, result["date"], request)}


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
    return {"result": result, "state": _state(user, date, request)}


class CancelIn(BaseModel):
    reason: str = Field(default="", max_length=30)
    note: str | None = Field(default=None, max_length=200)
    outcome: Literal["POSTPONED", "CANCELLED"] = "POSTPONED"


@app.post("/api/pending/{pending_id}/cancel")
def cancel_pending(pending_id: str, request: Request, body: CancelIn | None = None, date: str | None = None) -> dict:
    """Give up a request that was taken off the schedule and is waiting for a new slot (with a reason)."""
    user = _user(request, "book")
    data = body.model_dump() if body is not None else None
    result = _run(store.cancel_pending, pending_id, data=data, actor=_actor(user))
    return {"result": result, "state": _state(user, date, request)}


@app.post("/api/cases/{case_id}/cancel")
def cancel_booking(case_id: str, body: CancelIn, request: Request, date: str | None = None) -> dict:
    """Call off a booked case (งดผ่าตัด) with a reason: postpone it to the waiting list or give it up."""
    user = _user(request, "book")
    result = _run(store.cancel_booking, case_id, body.model_dump(), day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date, request)}


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
    return _run(admin.save_bed, body.model_dump(), code=code, actor=_actor(user),
                keep_type=not can(user.role, "hospital_data"))


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
        writer.writerow([hospital_io.csv_safe(v) for v in (
            c.case_id, local(c.start)[:10], c.room, c.surgeon, c.procedure, c.urgency, local(c.start),
            local(c.end), local(c.actual_start), local(c.actual_end), c.booked_mins,
            c.actual_mins if c.actual_mins is not None else "",
            c.actual_mins - c.booked_mins if c.actual_mins is not None else "", len(c.team))])
    safe = lambda d: d if d and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else None  # noqa: E731  (file name)
    name = f"orchestra-cases-{safe(date_from) or 'last30'}-{safe(date_to) or 'today'}.csv"
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


# ---------------------------------------------------------- steps and status board

class MilestoneIn(BaseModel):
    step: Literal["arrived", "in_room", "incision", "closure", "out_room", "recovery_out"]
    time: str | None = Field(default=None, max_length=5)  # HH:MM; empty = now
    clear: bool = False  # undo the latest step


@app.post("/api/cases/{case_id}/milestone")
def record_milestone(case_id: str, body: MilestoneIn, request: Request, date: str | None = None) -> dict:
    """Record a step of a case (holding, into the OR, incision, closure, out, left recovery) or undo the latest."""
    user = _user(request, "record_times")
    result = _run(store.milestone_update, case_id, body.model_dump(), day=date, actor=_actor(user))
    return {"result": result, "state": _state(user, date, request)}


@app.get("/api/board")
def board(request: Request, date: str | None = None, family: bool = False) -> dict:
    """The status board: each room's cases and where each one is now. Never patient names or HNs;
    ``family`` leaves out procedures and surgeons too (a waiting-room screen)."""
    from datetime import datetime

    user = _user(request, "view")
    state = _run(store.state, date)
    steps, ready = state.get("milestones") or {}, state.get("readiness") or {}
    rooms: dict[str, list] = {r["id"]: [] for r in state["options"]["rooms"]}
    for b in sorted(state["schedule"]["bookings"], key=lambda x: (x["or_id"], x["start_min"])):
        m = steps.get(b["case_id"]) or {}
        item = {"case_id": b["case_id"], "start": b["start"], "end": b["end"], "start_min": b["start_min"],
                "urgency": b["urgency"], **{k: m.get(k) for k in ("stage", "label", "phase", "since", "elapsed", "late_min")}}
        if not family:
            item |= {"procedure": b["procedure"], "surgeon": (b["team"][0] if b["team"] else {}).get("name"),
                     "readiness": (ready.get(b["case_id"]) or {}).get("status")}
        rooms.setdefault(b["or_id"], []).append(item)
    return {"date": state.get("date"), "now": datetime.now(_TZ).strftime("%H:%M"), "family": family,
            "hospital_name": (os.environ.get("HOSPITAL_NAME") or "").strip()[:120] or None,
            "rooms": [{"room": r, "cases": cases} for r, cases in rooms.items()],
            "steps_ready": state.get("milestones_ready", False), "viewer": user.role}


# ------------------------------------------------------------- weekly OR blocks

class BlockIn(BaseModel):
    room: str = Field(max_length=20)
    weekdays: list[int] = Field(max_length=7)
    start: str = Field(max_length=5)
    end: str = Field(max_length=5)
    owner_kind: Literal["SPECIALTY", "SURGEON"]
    specialty: str | None = Field(default=None, max_length=100)
    surgeon_id: str | None = Field(default=None, max_length=64)
    release_days: int = Field(default=2, ge=0, le=60)
    valid_from: str | None = Field(default=None, max_length=10)
    valid_to: str | None = Field(default=None, max_length=10)
    note: str | None = Field(default=None, max_length=200)


class BlockReleaseIn(BaseModel):
    day: str = Field(max_length=10)
    undo: bool = False


@app.get("/api/admin/blocks")
def admin_blocks(request: Request, day: str | None = None) -> dict:
    """Weekly OR blocks (and, with ``day``, that date's blocks and whether each is open to everyone)."""
    _, admin = _admin(request, "hospital_view")
    return _run(admin.blocks, day)


@app.post("/api/admin/blocks")
def admin_add_block(body: BlockIn, request: Request) -> dict:
    user, admin = _admin(request, "roster")
    return _run(admin.save_block, body.model_dump(), actor=_actor(user))


@app.patch("/api/admin/blocks/{block_id}")
def admin_edit_block(block_id: str, body: BlockIn, request: Request) -> dict:
    user, admin = _admin(request, "roster")
    return _run(admin.save_block, body.model_dump(), block_id=block_id, actor=_actor(user))


@app.delete("/api/admin/blocks/{block_id}")
def admin_delete_block(block_id: str, request: Request) -> dict:
    user, admin = _admin(request, "roster")
    return _run(admin.delete_block, block_id, actor=_actor(user))


@app.post("/api/admin/blocks/{block_id}/release")
def admin_release_block(block_id: str, body: BlockReleaseIn, request: Request) -> dict:
    """The owner gives one date of the block back to everyone early (``undo``: takes it back)."""
    user, admin = _admin(request, "roster")
    return _run(admin.release_block, block_id, body.day, undo=body.undo, actor=_actor(user))


# ------------------------------------------------------------- system status (admin)

class MigrateIn(BaseModel):
    confirm: bool = False


@app.get("/api/system/status")
def system_status_view(request: Request) -> dict:
    """SQL files applied or missing, settings (set or not, never values) and the go-live checklist."""
    _user(request, "users")
    out = _run(system_status.summary, store, notifier, intake, accounts)
    out["checks"] = out["checks"] + [c for c in (_two_factor_check(), _feedback_check(), his.check()) if c]
    return out


def _two_factor_check() -> dict | None:
    """Go-live checklist: the accounts that can see and export everything sign in with a code too."""
    if not os.environ.get("APP_PASSWORD"):
        return None
    problems, fine = [], []
    if os.environ.get("APP_TOTP_SECRET") and not env_totp_secret():
        problems.append("ค่า APP_TOTP_SECRET ใน Render ไม่ถูกต้อง (บัญชีหลักยังเข้าด้วยรหัสผ่านอย่างเดียว)")
    elif not env_totp_secret():
        problems.append("บัญชีหลักยังไม่เปิด (ตั้งค่าที่หน้าความปลอดภัยของบัญชี แล้วใส่ APP_TOTP_SECRET ใน Render)")
    else:
        fine.append("บัญชีหลักเปิดแล้ว")
    if store.mode == "supabase":
        try:
            ready = accounts.store.two_factor_ready()
            users = [u for u in accounts.store.list() if u.is_active] if ready else []
        except AuthError:
            return None
        if not ready:
            problems.append("ยังไม่ได้รัน SQL ไฟล์ 018 (ผู้ดูแลระบบอื่นยังเข้าด้วยรหัสผ่านอย่างเดียว)")
        else:
            waiting = [u for u in users if u.must_setup_2fa]
            on = sum(1 for u in users if u.totp_enabled)
            if waiting:
                problems.append(f"ยังไม่ตั้งค่า {len(waiting)} บัญชี (ต้องตั้งเมื่อเข้าระบบครั้งถัดไป)")
            fine.append(f"เปิดแล้ว {on} จาก {len(users)} บัญชี")
    return {"label": "การยืนยันตัวตน 2 ชั้น", "status": "warn" if problems else "ok",
            "detail": " · ".join(problems + fine), "link": "/security"}


def _feedback_check() -> dict:
    view = feedback.overview()
    new = [f"เรื่องใหม่ที่ผู้ใช้แจ้ง {view['new_reports']} เรื่อง"] if view["new_reports"] else []
    new += [f"ข้อผิดพลาดของระบบ {view['new_errors']} แบบ"] if view["new_errors"] else []
    seen = sum(1 for r in view["reports"] if r["status"] == "SEEN")
    detail = " · ".join(new) or "ไม่มีเรื่องใหม่"
    if seen:
        detail += f" · รับทราบแล้ว รอแก้ {seen} เรื่อง"
    if not view["ready"]:
        detail += " · ยังไม่ได้รัน SQL ไฟล์ 016: เก็บไว้ชั่วคราว หายเมื่อเซิร์ฟเวอร์เริ่มใหม่"
    return {"label": "เรื่องที่ผู้ใช้แจ้ง และข้อผิดพลาดของระบบ", "status": "warn" if new or not view["ready"] else "ok",
            "detail": detail, "link": "#feedback"}


@app.post("/api/system/migrate")
def system_migrate(body: MigrateIn, request: Request) -> dict:
    """Run the SQL files the database still lacks, in order (each in its own transaction)."""
    user = _user(request, "users")
    if store.mode != "supabase":
        raise HTTPException(status_code=400, detail="ใช้ได้เมื่อเชื่อมฐานข้อมูล Supabase แล้วเท่านั้น")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="กรุณายืนยันก่อนรัน SQL")
    results = _run(system_status.run_missing, store.db)
    system_status.log_run(store, results, _actor(user))
    return {"results": results, "status": _run(system_status.summary, store, notifier, intake, accounts)}


# ------------------------------------------------------------- hospital information system (HIS)

class HisRequestIn(BaseModel):
    """A surgery request (order) from the HIS. Sending the same ``his_ref`` again updates it (safe to resend)."""
    his_ref: str = Field(min_length=1, max_length=100, description="เลขที่ใบขอผ่าตัดของ HIS (ไม่ซ้ำ)")
    action: Literal["upsert", "cancel"] = Field(default="upsert", description="upsert = สร้างหรือแก้ไข, cancel = ยกเลิก")
    hn: str | None = Field(default=None, max_length=50)
    patient_name: str | None = Field(default=None, max_length=150)
    surgeon_code: str | None = Field(default=None, max_length=100, description="รหัสแพทย์ของ HIS หรือรหัสพนักงานในระบบนี้")
    surgeon_name: str | None = Field(default=None, max_length=200, description="ชื่อแพทย์ (ช่วยจับคู่รหัส)")
    procedure_code: str | None = Field(default=None, max_length=100, description="รหัส ICD-9-CM หรือรหัสหัตถการของ HIS")
    procedure_name: str | None = Field(default=None, max_length=200)
    urgency: Literal["ELECTIVE", "URGENT", "EMERGENCY"] = "ELECTIVE"
    duration_mins: int | None = Field(default=None, ge=15, le=720, description="ไม่ส่ง = ใช้ preference card / เวลามาตรฐาน")
    requested_date: str | None = Field(default=None, pattern=DATE, description="วันที่แพทย์ขอ")
    due_date: str | None = Field(default=None, pattern=DATE, description="ควรผ่าตัดภายในวันที่")
    requires_icu: bool | None = None
    airborne: bool = False


class HisBatchIn(BaseModel):
    requests: list[HisRequestIn] = Field(min_length=1, max_length=his_mod.BATCH_MAX)


def _his_key(request: Request) -> str | None:
    bearer = request.headers.get("authorization") or ""
    if bearer.lower().startswith("bearer " + his_mod.KEY_PREFIX):
        return bearer
    return request.headers.get("x-api-key") or bearer or None


def _his_gate(request: Request) -> JSONResponse | None:
    """Every call under the HIS prefix: the key is checked (and counted) before the body is read, so
    nobody without a key can make the server read or parse anything."""
    try:
        request.state.his_client = his.authenticate(_his_key(request), None, _address(request))
    except his_mod.HisAuthError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)
    except StoreError as exc:  # HisUnavailable, or the database cannot be reached
        return JSONResponse({"detail": str(exc)}, status_code=503)
    return None


def _his_client(request: Request, scope: str) -> his_mod.Client:
    client = getattr(request.state, "his_client", None)
    if client is None:  # never: the middleware checked the key
        raise HTTPException(status_code=401, detail="ต้องส่ง API key")
    if scope not in client.scopes:
        raise HTTPException(status_code=403, detail=f"key นี้ไม่มีสิทธิ์ {scope}")
    return client


def _his_run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except his_mod.HisAuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except his_mod.HisUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except StoreError as exc:
        raise HTTPException(status_code=404 if "ไม่พบ" in str(exc) else 400, detail=str(exc)) from exc
    except psycopg.DataError as exc:  # a value the database cannot store as given
        raise HTTPException(status_code=400, detail="ข้อมูลบางช่องไม่ถูกต้อง (อักขระหรือความยาว)") from exc


@app.get("/api/his/v1/ping", tags=["HIS"])
def his_ping(request: Request) -> dict:
    """Check the key and the connection (any scope)."""
    client = _his_client(request, next(iter(request.state.his_client.scopes), ""))
    return {"ok": True, "key": client.name, "scopes": list(client.scopes)}


@app.post("/api/his/v1/surgery-requests", tags=["HIS"])
def his_request(body: HisRequestIn, request: Request) -> dict:
    """Create, change or cancel one surgery request. Unknown doctor / procedure codes wait for the admin to match."""
    client = _his_client(request, "requests:write")
    return _his_run(his.receive, body.model_dump(), client=client)


@app.post("/api/his/v1/surgery-requests/batch", tags=["HIS"])
def his_batch(body: HisBatchIn, request: Request) -> dict:
    """Up to 200 requests at once (each is applied on its own)."""
    client = _his_client(request, "requests:write")
    _his_run(his.charge, client, len(body.requests) - 1)  # the volume limit counts requests, not calls
    results = []
    for item in body.requests:  # one failing request does not stop the rest
        try:
            results.append(_his_run(his.receive, item.model_dump(), client=client))
        except HTTPException as exc:
            if exc.status_code == 503:
                raise
            results.append({"his_ref": item.his_ref, "status": "REJECTED", "status_label": his_mod.STATUS_TH["REJECTED"],
                            "detail": exc.detail})
        except Exception as exc:  # recorded for the admin; the HIS may send this one again later
            feedback.record_error("server", "POST /api/his/v1/surgery-requests/batch", fb.describe(exc))
            results.append({"his_ref": item.his_ref, "status": "ERROR", "status_label": "ระบบขัดข้อง ส่งรายการนี้ใหม่ภายหลัง",
                            "detail": "เกิดข้อผิดพลาดในระบบ"})
    return {"results": results}


@app.get("/api/his/v1/surgery-requests/{his_ref:path}", tags=["HIS"])
def his_request_status(his_ref: str, request: Request) -> dict:
    """What became of a request: waiting, booked (day, time, room), in progress, done or cancelled."""
    client = _his_client(request, "schedule:read")
    return _his_run(his.request_status, his_ref, client, _address(request))


@app.get("/api/his/v1/schedule", tags=["HIS"])
def his_schedule(request: Request, date: str | None = Query(default=None, pattern=DATE)) -> dict:
    """Every booked case of a day (default today), with the HIS order number when it came from the HIS."""
    client = _his_client(request, "schedule:read")
    return _his_run(his.schedule, date, client, _address(request))


class HisKeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    scopes: list[Literal["requests:write", "schedule:read"]] = Field(min_length=1)


class HisMappingIn(BaseModel):
    kind: Literal["surgeon", "procedure"]
    his_code: str = Field(min_length=1, max_length=100)
    target_id: str = Field(max_length=64)
    label: str | None = Field(default=None, max_length=200)


@app.get("/api/admin/his")
def his_overview(request: Request) -> dict:
    _admin(request, "hospital_data")
    return _run(his.overview)


@app.post("/api/admin/his/keys")
def his_key_create(body: HisKeyIn, request: Request) -> dict:
    user, _ = _admin(request, "hospital_data")
    return {"key": _run(his.create_key, body.name, body.scopes, _actor(user)), "overview": _run(his.overview)}


@app.delete("/api/admin/his/keys/{key_id}")
def his_key_revoke(key_id: str, request: Request) -> dict:
    user, _ = _admin(request, "hospital_data")
    _run(his.revoke_key, key_id, _actor(user))
    return {"overview": _run(his.overview)}


@app.put("/api/admin/his/mappings")
def his_mapping_set(body: HisMappingIn, request: Request) -> dict:
    user, _ = _admin(request, "hospital_data")
    result = _run(his.set_mapping, body.kind, body.his_code, body.target_id, body.label, _actor(user))
    return {"result": result, "overview": _run(his.overview)}


@app.delete("/api/admin/his/mappings/{kind}/{his_code}")
def his_mapping_delete(kind: Literal["surgeon", "procedure"], his_code: str, request: Request) -> dict:
    user, _ = _admin(request, "hospital_data")
    _run(his.delete_mapping, kind, his_code, _actor(user))
    return {"overview": _run(his.overview)}


@app.post("/api/admin/his/inbox/{message_id}/retry")
def his_retry(message_id: int, request: Request) -> dict:
    user, _ = _admin(request, "hospital_data")
    return {"result": _run(his.retry, message_id, _actor(user)), "overview": _run(his.overview)}


@app.post("/api/admin/his/test")
def his_test(body: HisRequestIn, request: Request) -> dict:
    """The admin sends a request as if from the HIS (same pipeline, no key), to try the connection."""
    user, _ = _admin(request, "hospital_data")
    return {"result": _run(his.receive, body.model_dump(), source="test", actor=_actor(user)), "overview": _run(his.overview)}


# ------------------------------------------------------------- problem reports and errors

class FeedbackIn(BaseModel):
    kind: Literal["PROBLEM", "IDEA", "QUESTION"] = "PROBLEM"
    message: str = Field(min_length=1, max_length=fb.MAX_MESSAGE)
    page: str | None = Field(default=None, max_length=500)


class ClientErrorIn(BaseModel):
    message: str = Field(max_length=2000)
    where: str | None = Field(default=None, max_length=500)
    page: str | None = Field(default=None, max_length=500)


class FeedbackUpdateIn(BaseModel):
    status: Literal["NEW", "SEEN", "DONE"]
    note: str | None = Field(default=None, max_length=fb.MAX_NOTE)


@app.post("/api/feedback")
def send_feedback(body: FeedbackIn, request: Request) -> dict:
    """Anyone signed in: a problem, a suggestion or a question (page and time attached, no patient data)."""
    user = request.state.user
    result = _run(feedback.submit, user, body.kind, body.message, body.page, request.headers.get("user-agent"))
    return {"result": result, "mine": feedback.mine(user)}


@app.get("/api/feedback/mine")
def my_feedback(request: Request) -> dict:
    return {"mine": feedback.mine(request.state.user)}


@app.post("/api/client-errors")
def client_error(body: ClientErrorIn, request: Request) -> dict:
    """An error in the browser (sent by the page itself)."""
    recorded = feedback.record_error("browser", body.where or "-", body.message, user=request.state.user, page=body.page)
    return {"recorded": recorded}


@app.get("/api/feedback")
def feedback_overview(request: Request, done: bool = False) -> dict:
    """Admin: reports from users and errors of the system, newest first (open ones unless ``done``)."""
    _user(request, "users")
    return feedback.overview(show_done=done)


@app.patch("/api/feedback/{what}/{item_id}")
def feedback_update(what: Literal["report", "error"], item_id: int, body: FeedbackUpdateIn, request: Request) -> dict:
    user = _user(request, "users")
    if what == "error" and body.status == "SEEN":
        raise HTTPException(status_code=400, detail="ข้อผิดพลาดมีสถานะ ใหม่ หรือ แก้แล้ว เท่านั้น")
    result = _run(feedback.update, what, item_id, body.status, body.note, _actor(user))
    return {"result": result, "overview": feedback.overview()}


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    """Anything not handled: listed for the admin, and a plain message for the person."""
    user = getattr(request.state, "user", None)
    route = getattr(request.scope.get("route"), "path", None) or "(ก่อนถึง route)"  # the pattern, not the caller's path
    await run_in_threadpool(feedback.record_error, "server", f"{request.method} {route}", fb.describe(exc), user=user)
    return JSONResponse({"detail": "เกิดข้อผิดพลาดในระบบ ผู้ดูแลระบบได้รับบันทึกแล้ว กรุณาลองใหม่อีกครั้ง"}, status_code=500)


# ------------------------------------------------------------- PDPA (admin)

class RetentionIn(BaseModel):
    patient_days: int | None = Field(default=None, ge=privacy.MIN_DAYS, le=privacy.MAX_DAYS)
    access_days: int | None = Field(default=None, ge=privacy.MIN_DAYS, le=privacy.MAX_DAYS)


@app.get("/api/privacy/access")
def privacy_access(request: Request, username: str | None = None, date_from: str | None = None,
                   date_to: str | None = None, case: str | None = Query(default=None, max_length=40),
                   hn: str | None = Query(default=None, max_length=50)) -> dict:
    """Who was shown patients' HN / names, newest first (looking up an HN is itself recorded)."""
    user = _user(request, "users")
    out = _run(access.entries, username=username, date_from=date_from, date_to=date_to, case=case, hn=hn,
               user=user, address=_address(request))
    out["users"] = sorted({u.username for u in _run(_safe_users)} | {e["username"] for e in out["entries"]})
    return out


def _safe_users() -> list:
    try:
        return accounts.store.list()
    except AuthError:  # no app_users table yet
        return []


@app.get("/api/privacy/retention")
def privacy_retention(request: Request) -> dict:
    _user(request, "users")
    return _run(privacy.retention, store)


@app.post("/api/privacy/retention")
def privacy_set_retention(body: RetentionIn, request: Request) -> dict:
    user = _user(request, "users")
    return _run(privacy.set_retention, store, body.patient_days, body.access_days, actor=_actor(user))


@app.post("/api/privacy/apply")
def privacy_apply(body: MigrateIn, request: Request) -> dict:
    """Remove identity from cases past the retention period now (instead of waiting for the nightly run)."""
    user = _user(request, "users")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="กรุณายืนยันก่อนลบข้อมูล")
    result = _run(privacy.apply, store, actor=_actor(user))
    return {"result": result, "retention": _run(privacy.retention, store)}


@app.get("/api/system/backup.zip")
def system_backup(request: Request) -> StreamingResponse:
    """Every table as CSV in a ZIP, with restore scripts (admin). It holds every patient's HN and name,
    so each download goes into the PDPA access log."""
    user = _user(request, "users")
    if store.mode != "supabase":
        raise HTTPException(status_code=400, detail="โหมดสาธิตไม่มีฐานข้อมูลให้สำรอง")
    out, manifest = _run(backup.dump, store.db.url)
    size = out.seek(0, 2)
    out.seek(0)
    if not access.record(user, "backup", None, [], _address(request), patients=backup.patients_in(manifest)):
        out.close()  # every patient's identity leaves with this file: no record, no file
        raise HTTPException(status_code=503, detail="บันทึกการเข้าถึงข้อมูลผู้ป่วยไม่ได้ (ต้องรัน SQL ไฟล์ 013) จึงยังดาวน์โหลดสำรองข้อมูลไม่ได้")
    system_status.note_backup(store, manifest, size, _actor(user))
    name = f"orchestra-backup-{store.db.today().isoformat()}.zip"

    def chunks():
        try:
            while chunk := out.read(1 << 16):
                yield chunk
        finally:
            out.close()

    return StreamingResponse(chunks(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{name}"',
                                      "Content-Length": str(size)})


# ------------------------------------------------------------- past surgeries from Excel (forecast)

def _file(body: ImportIn) -> bytes:
    text = body.content.split(",", 1)[1] if body.content.startswith("data:") else body.content
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="อ่านไฟล์ไม่ได้ (ข้อมูลไฟล์ไม่ถูกต้อง)") from exc


@app.get("/api/admin/history")
def admin_history(request: Request) -> dict:
    """Past surgeries imported for the duration forecast: totals and each imported file."""
    _, admin = _admin(request, "hospital_data")
    return _run(history_io.overview, admin)


@app.get("/api/admin/history.xlsx")
def admin_history_template(request: Request) -> Response:
    _, admin = _admin(request, "hospital_data")
    data = _run(history_io.template, admin)
    return Response(data, media_type=XLSX, headers={
        "Content-Disposition": 'attachment; filename="orchestra-surgical-history.xlsx"', "Cache-Control": "no-store"})


@app.post("/api/admin/history/import")
def admin_history_import(body: ImportIn, request: Request) -> dict:
    """Check an uploaded history file; with ``commit`` save its right rows (wrong ones are listed, not saved)."""
    user, admin = _admin(request, "hospital_data")
    return _run(history_io.import_history, admin, body.filename, _file(body), commit=body.commit, actor=_actor(user))


@app.delete("/api/admin/history/{batch_id}")
def admin_history_delete(batch_id: str, request: Request) -> dict:
    """Remove one imported file's cases (e.g. the wrong file was uploaded)."""
    user, admin = _admin(request, "hospital_data")
    return _run(history_io.delete_batch, admin, batch_id, actor=_actor(user))
