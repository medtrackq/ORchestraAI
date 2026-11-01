"""Two-step sign-in: a 6-digit code from an authenticator app after the password (and recovery codes).

The database tests run against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

import base64

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import twofactor as tf
from app.auth import Accounts, AuthError, DbUserStore, Signer
from app.main import accounts, app
from test_app import OWNER, fresh, make_user, owner, session  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, _rows, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")


@pytest.fixture()
def clock(monkeypatch):
    """The authenticator's 30-second step, moved by hand (a code works once, so each sign-in needs a new one)."""
    now = {"step": 60_000_000}
    monkeypatch.setattr(tf, "step_now", lambda at=None: now["step"])
    return now


def _code(secret: str, clock: dict, ahead: int = 0) -> str:
    return tf.code_at(secret, clock["step"] + ahead)


def _turn_on(client: TestClient, clock: dict) -> tuple[str, list[str]]:
    setup = client.post("/api/2fa/setup", json={})
    assert setup.status_code == 200, setup.text
    body = setup.json()
    assert body["qr"].startswith("<svg") and body["uri"].startswith("otpauth://totp/ORchestraAI")
    done = client.post("/api/2fa/enable", json={"setup_token": body["setup_token"], "code": _code(body["secret"], clock)})
    assert done.status_code == 200, done.text
    return body["secret"], done.json().get("recovery_codes", [])


# ------------------------------------------------------------------ the codes

def test_codes_follow_rfc_6238():
    secret = base64.b32encode(b"12345678901234567890").decode().rstrip("=")
    for when, expected in ((59, "94287082"), (1111111109, "07081804"), (1234567890, "89005924"), (2000000000, "69279037")):
        assert tf.code_at(secret, tf.step_now(when)) == expected[-6:]
    now = 1_800_000_000
    step = tf.step_now(now)
    assert tf.match(secret, tf.code_at(secret, step - 1), now=now) == step - 1  # a phone 30 seconds slow
    assert tf.match(secret, tf.code_at(secret, step - 2), now=now) is None  # older is refused
    assert tf.match(secret, tf.code_at(secret, step), now=now, last_step=step) is None  # used already
    assert tf.match(secret, "12 34 56", now=now) is None and tf.match("not base32!", "123456", now=now) is None
    codes = tf.new_recovery_codes()
    assert len(set(codes)) == 10 and all(len(c) == 11 and c[5] == "-" for c in codes)
    assert tf.hash_recovery(codes[0].upper().replace("-", " ")) == tf.hash_recovery(codes[0])  # as typed from paper
    assert tf.valid_secret(tf.new_secret()) and not tf.valid_secret("ABC")


# ------------------------------------------------------------------ demo mode (accounts in memory)

def test_turn_on_then_sign_in_with_a_code_or_a_recovery_code(owner, clock):
    nurse = make_user(owner, "nurse2fa", "staff")
    status = nurse.get("/api/2fa").json()
    assert (status["enabled"], status["required"], status["must_setup"]) == (False, False, False)
    setup = nurse.post("/api/2fa/setup", json={}).json()
    wrong = nurse.post("/api/2fa/enable", json={"setup_token": setup["setup_token"], "code": "000000"})
    assert wrong.status_code == 401 and "เวลาในมือถือ" in wrong.json()["detail"]
    done = nurse.post("/api/2fa/enable", json={"setup_token": setup["setup_token"], "code": _code(setup["secret"], clock)})
    assert done.status_code == 200 and len(done.json()["recovery_codes"]) == 10
    secret, recovery = setup["secret"], done.json()["recovery_codes"]
    assert nurse.get("/api/me").json()["user"]["totp_enabled"]  # this device carries on

    c = TestClient(app, base_url="https://testserver")
    first = c.post("/api/login", json={"username": "nurse2fa", "password": "Real-Pass-2026!"})
    assert first.status_code == 200 and first.json()["needs_code"] and "user" not in first.json()
    assert c.get("/api/state").status_code == 401  # the password alone opens nothing
    pending = c.cookies.get("orchestra_2fa")
    stolen = TestClient(app, base_url="https://testserver", cookies={"orchestra_session": pending})
    assert stolen.get("/api/me").status_code == 401  # the in-between token is not a session
    assert c.post("/api/login/code", json={"code": "123456"}).status_code == 401
    used = _code(secret, clock)  # the step used to switch it on
    assert c.post("/api/login/code", json={"code": used}).status_code == 401  # each code works once
    clock["step"] += 1
    ok = c.post("/api/login/code", json={"code": _code(secret, clock)})
    assert ok.status_code == 200 and ok.json()["used"] == "app"
    assert c.get("/api/state").status_code == 200

    lost_phone = TestClient(app, base_url="https://testserver")
    lost_phone.post("/api/login", json={"username": "nurse2fa", "password": "Real-Pass-2026!"})
    back = lost_phone.post("/api/login/code", json={"code": recovery[0].upper()})
    assert back.status_code == 200 and (back.json()["used"], back.json()["recovery_left"]) == ("recovery", 9)
    again = TestClient(app, base_url="https://testserver")
    again.post("/api/login", json={"username": "nurse2fa", "password": "Real-Pass-2026!"})
    assert again.post("/api/login/code", json={"code": recovery[0]}).status_code == 401  # once only
    assert lost_phone.get("/api/2fa").json()["recovery_left"] == 9

    clock["step"] += 1
    fresh_codes = lost_phone.post("/api/2fa/recovery", json={"code": _code(secret, clock)})
    assert fresh_codes.status_code == 200 and recovery[1] not in fresh_codes.json()["recovery_codes"]
    assert lost_phone.post("/api/2fa/disable", json={"code": "999999"}).status_code == 401
    clock["step"] += 1
    assert lost_phone.post("/api/2fa/disable", json={"code": _code(secret, clock)}).status_code == 200
    plain = TestClient(app, base_url="https://testserver")
    assert "user" in plain.post("/api/login", json={"username": "nurse2fa", "password": "Real-Pass-2026!"}).json()


def test_wrong_codes_are_limited_and_the_wait_is_short(owner, clock):
    nurse = make_user(owner, "nurse5", "staff")
    secret, _ = _turn_on(nurse, clock)
    c = TestClient(app, base_url="https://testserver")
    c.post("/api/login", json={"username": "nurse5", "password": "Real-Pass-2026!"})
    for _ in range(5):
        assert c.post("/api/login/code", json={"code": "000000"}).status_code == 401
    clock["step"] += 1
    assert c.post("/api/login/code", json={"code": _code(secret, clock)}).status_code == 429  # even the right one
    late = TestClient(app, base_url="https://testserver")
    assert late.post("/api/login/code", json={"code": "123456"}).status_code == 401  # no password step: nothing


def test_the_owner_account_uses_the_render_setting(owner, clock, monkeypatch):
    setup = owner.post("/api/2fa/setup", json={}).json()
    assert setup["owner_account"]
    done = owner.post("/api/2fa/enable", json={"setup_token": setup["setup_token"], "code": _code(setup["secret"], clock)})
    assert done.json() == {"owner_account": True, "secret": setup["secret"]}  # to put in Render
    assert not owner.get("/api/2fa").json()["enabled"]  # nothing changes until it is set there

    monkeypatch.setenv("APP_TOTP_SECRET", setup["secret"])
    assert owner.get("/api/me").status_code == 401  # setting it signs the owner out everywhere
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/api/login", json={"username": OWNER[0], "password": OWNER[1]}).json()["needs_code"]
    clock["step"] += 1
    assert c.post("/api/login/code", json={"code": _code(setup["secret"], clock)}).status_code == 200
    assert c.post("/api/2fa/disable", json={"code": "123456"}).status_code == 401  # switched off in Render only
    monkeypatch.setenv("APP_TOTP_SECRET", "not a secret")
    assert "user" in session(*OWNER).get("/api/me").json()  # a broken setting never locks the owner out


def test_demo_mode_does_not_force_it(owner):
    admin = make_user(owner, "admin2", "admin")
    me = admin.get("/api/me").json()["user"]
    assert me["role"] == "admin" and not me["must_setup_2fa"]  # accounts in memory are for trying the app
    assert admin.get("/api/state").status_code == 200


# ------------------------------------------------------------------ database (real accounts)

@pytest.fixture()
def db_accounts(store, monkeypatch):
    monkeypatch.setattr(accounts, "store", DbUserStore(store.db))
    return accounts


@needs_db
def test_admins_must_set_it_up_before_anything_else(db_accounts, owner, clock, db_url):
    admin = make_user(owner, "headnurse", "admin")
    me = admin.get("/api/me").json()["user"]
    assert me["must_setup_2fa"] and not me["totp_enabled"]
    blocked = admin.get("/api/users")
    assert blocked.status_code == 403 and "2 ชั้น" in blocked.json()["detail"]
    page = admin.get("/admin", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/security"
    assert admin.get("/security").status_code == 200

    secret, recovery = _turn_on(admin, clock)
    assert admin.get("/api/users").status_code == 200
    stored = _rows(db_url, "SELECT totp_secret, totp_last_step, recovery_codes FROM app_users WHERE username = 'headnurse'")[0]
    assert stored[0] == secret and stored[1] == clock["step"] and len(stored[2]) == 10
    assert recovery[0] not in str(stored[2])  # only hashes are kept
    clock["step"] += 1
    assert admin.post("/api/2fa/disable", json={"code": _code(secret, clock)}).status_code == 401  # admins keep it

    # two sign-ins with the same code (e.g. someone watching over a shoulder): only the first works
    a, b = TestClient(app, base_url="https://testserver"), TestClient(app, base_url="https://testserver")
    for c in (a, b):
        assert c.post("/api/login", json={"username": "headnurse", "password": "Real-Pass-2026!"}).json()["needs_code"]
    clock["step"] += 1
    code = _code(secret, clock)
    assert a.post("/api/login/code", json={"code": code}).status_code == 200
    assert b.post("/api/login/code", json={"code": code}).status_code == 401
    assert b.post("/api/login/code", json={"code": recovery[3]}).json()["recovery_left"] == 9
    assert _rows(db_url, "SELECT jsonb_array_length(recovery_codes) FROM app_users WHERE username = 'headnurse'") == [(9,)]

    # a new phone: the old code (or a recovery code) first
    setup = a.post("/api/2fa/setup", json={}).json()
    clock["step"] += 1
    move = {"setup_token": setup["setup_token"], "code": _code(setup["secret"], clock)}
    assert a.post("/api/2fa/enable", json=move).status_code == 401
    moved = a.post("/api/2fa/enable", json={**move, "current_code": recovery[4]})
    assert moved.status_code == 200 and len(moved.json()["recovery_codes"]) == 10
    assert b.get("/api/me").status_code == 401  # other devices are signed out


@needs_db
def test_an_admin_resets_someone_who_lost_their_phone(db_accounts, owner, clock):
    nurse = make_user(owner, "nurse9", "staff")
    _turn_on(nurse, clock)
    users = owner.get("/api/users").json()["users"]
    target = next(u for u in users if u["username"] == "nurse9")
    assert target["totp_enabled"] and "totp_secret" not in target
    scheduler = make_user(owner, "sched9", "scheduler")
    assert scheduler.post(f"/api/users/{target['user_id']}/reset-2fa", json={}).status_code == 403
    out = owner.post(f"/api/users/{target['user_id']}/reset-2fa", json={})
    assert out.status_code == 200 and not out.json()["user"]["totp_enabled"]
    assert nurse.get("/api/me").status_code == 401  # signed out everywhere
    assert "user" in session("nurse9", "Real-Pass-2026!").get("/api/me").json()  # password only again
    assert owner.post(f"/api/users/{target['user_id']}/reset-2fa", json={}).status_code == 400  # nothing to reset


@needs_db
def test_works_before_migration_018(store, db_url, owner, monkeypatch):
    with psycopg.connect(db_url, autocommit=True) as conn:
        for col in ("totp_secret", "totp_enabled_at", "totp_last_step", "recovery_codes"):
            conn.execute(f"ALTER TABLE app_users DROP COLUMN {col}")
    monkeypatch.setattr(accounts, "store", DbUserStore(store.db))
    admin = make_user(owner, "admin3", "admin")
    assert not admin.get("/api/me").json()["user"]["must_setup_2fa"]  # not forced before the SQL file
    assert admin.get("/api/users").status_code == 200
    r = admin.post("/api/2fa/setup", json={})
    assert r.status_code == 400 and "018" in r.json()["detail"]
    direct = Accounts(DbUserStore(store.db), Signer(b"k" * 32))
    user, token = direct.login("admin3", "Real-Pass-2026!", "10.0.0.1")
    assert direct.user_from_token(token).username == "admin3"
    with pytest.raises(AuthError, match="018"):
        direct.store.enable_totp(user.user_id, tf.new_secret(), 1, [])


@needs_db
def test_the_status_page_lists_who_has_it(db_accounts, owner, clock, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main.store, "mode", "supabase", raising=False)
    admin = make_user(owner, "admin4", "admin")
    check = main._two_factor_check()
    assert check["status"] == "warn" and "บัญชีหลักยังไม่เปิด" in check["detail"] and "ยังไม่ตั้งค่า 1 บัญชี" in check["detail"]
    _turn_on(admin, clock)
    monkeypatch.setenv("APP_TOTP_SECRET", tf.new_secret())
    check = main._two_factor_check()
    assert check["status"] == "ok" and "เปิดแล้ว 1 จาก 1 บัญชี" in check["detail"]
