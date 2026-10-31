"""ORchestraAI web app: API + web UI in one service (deployed on Render).

Run locally:
    uvicorn app.main:app --reload

Environment:
    DATABASE_URL       Supabase Postgres connection string (Session pooler). Without it
                       the app runs in demo mode on synthetic in-memory data.
    APP_TIMEZONE       planning time zone (default Asia/Bangkok)
    APP_PASSWORD       password for the login prompt (no password = open access)
    APP_USERNAME       login name (default "orchestra")
    SOLVER_WORKERS     CP-SAT worker threads (default 2)
    SOLVER_TIME_LIMIT  seconds per solve (default 10)
"""
from __future__ import annotations

import base64
import binascii
import os
import secrets
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from .store import ScheduleStore, StoreError

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
else:
    store = ScheduleStore(**_solver)
app = FastAPI(title="ORchestraAI", docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """HTTP Basic login for every page and API call except the health check."""
    password = os.environ.get("APP_PASSWORD", "")
    if password and request.url.path != "/api/health":
        username = os.environ.get("APP_USERNAME", "orchestra")
        if not _authorized(request.headers.get("authorization", ""), username, password):
            return Response(
                "Login required", status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="ORchestraAI", charset="UTF-8"'},
            )
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _authorized(header: str, username: str, password: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    return secrets.compare_digest(user.encode(), username.encode()) & secrets.compare_digest(
        given.encode(), password.encode()
    )


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


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except StoreError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/flow", include_in_schema=False)
def flow() -> FileResponse:
    return FileResponse(FLOW_PAGE)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "mode": store.mode}


@app.get("/api/state")
def state(date: str | None = None) -> dict:
    """Schedule, resources and activity for one planning day (default: today)."""
    return _run(store.state, date)


@app.post("/api/slot-check")
def slot_check(body: SlotIn, date: str | None = None) -> dict:
    """Ask the agents about a time; nothing is booked."""
    return _run(store.slot_check, body.case.model_dump(), body.start, commit=False, day=date)


@app.post("/api/bookings")
def book(body: SlotIn, date: str | None = None) -> dict:
    """Same check, then lock the slot that every agent accepts."""
    result = _run(store.slot_check, body.case.model_dump(), body.start, commit=True, day=date)
    return {"result": result, "state": _run(store.state, date)}


@app.post("/api/emergency")
def emergency(body: EmergencyIn, date: str | None = None) -> dict:
    result = _run(store.emergency, body.case.model_dump(), body.now, day=date)
    return {"result": result, "state": _run(store.state, date)}


@app.post("/api/overrun")
def overrun(body: OverrunIn, date: str | None = None) -> dict:
    result = _run(store.overrun, body.case_id, body.new_duration_mins, body.now, day=date)
    return {"result": result, "state": _run(store.state, date)}


@app.post("/api/reset")
def reset() -> dict:
    """Demo mode only: back to the synthetic demo day."""
    if store.mode != "demo":
        raise HTTPException(status_code=400, detail="ใช้ได้เฉพาะโหมดข้อมูลจำลอง")
    store.reset()
    return {"state": store.state()}


@app.post("/api/seed-demo")
def seed_demo() -> dict:
    """Supabase mode only: fill an EMPTY database with the synthetic demo hospital."""
    if store.mode != "supabase":
        raise HTTPException(status_code=400, detail="ใช้ได้เฉพาะเมื่อเชื่อม Supabase")
    result = _run(store.seed_demo)
    return {"result": result, "state": _run(store.state, result["date"])}
