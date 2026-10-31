"""When DATABASE_URL is wrong the app must explain it, not answer 500."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth import DB_DOWN, Accounts, DbUserStore, LoginLimiter, Signer
from app.db import Database, explain_connection_error
from app.db_store import DatabaseStore
from app.store import StoreError

UNREACHABLE = "postgresql://postgres:secret@127.0.0.1:1/postgres"  # nothing listens on port 1


@pytest.mark.parametrize("message, expected", [
    ('FATAL:  password authentication failed for user "postgres.abcd"', "รหัสผ่านใน DATABASE_URL"),
    ("FATAL:  Tenant or user not found", "postgres.<project-ref>"),
    ("connection to server at \"db.abcd.supabase.co\" (2406:da18::1), port 5432 failed: Network is unreachable",
     "Session pooler"),
    ('could not translate host name "[YOUR-HOST]" to address', "Session pooler"),
    ("invalid dsn: missing \"=\" after \"postgres\" in connection info string", "postgresql://"),
    ('invalid URI query parameter: "pgbouncer"', "ตัวอักษรภาษาอังกฤษกับตัวเลข"),
    ("connection timeout expired", "pause"),
])
def test_driver_errors_become_fixable_messages(message, expected):
    text = explain_connection_error(Exception(message))
    assert expected in text
    assert "secret" not in text and "abcd" not in text


def test_owner_can_still_sign_in_and_others_get_503(monkeypatch):
    monkeypatch.setenv("APP_PASSWORD", "owner-secret-123")
    monkeypatch.delenv("APP_USERNAME", raising=False)
    accounts = Accounts(DbUserStore(Database(UNREACHABLE)), Signer(b"k" * 32))
    accounts.limiter = LoginLimiter()
    monkeypatch.setattr(main, "accounts", accounts)
    client = TestClient(main.app, base_url="https://testserver")

    r = client.post("/api/login", json={"username": "someone", "password": "whatever"})
    assert r.status_code == 503 and r.json()["detail"] == DB_DOWN

    assert client.post("/api/login", json={"username": "orchestra", "password": "owner-secret-123"}).status_code == 200
    assert client.get("/api/me").status_code == 200
    assert client.get("/api/users").status_code in (400, 503)  # explained, not a crash


def test_schedule_store_explains_the_connection_problem():
    with pytest.raises(StoreError, match="เชื่อมต่อฐานข้อมูลไม่ได้"):
        DatabaseStore(UNREACHABLE).state()


@pytest.mark.parametrize("url, expected", [
    ("postgresql://postgres.abcd:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres",
     "[YOUR-PASSWORD]"),
    ("postgresql://postgres.abcd:p@ss#1@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres", "อักขระพิเศษ"),
])
def test_common_url_mistakes_are_named(url, expected):
    with pytest.raises(StoreError, match="เชื่อมต่อฐานข้อมูลไม่ได้") as info:
        DatabaseStore(url).state()
    assert expected in str(info.value)
    assert "p@ss" not in str(info.value)


GOOD = "postgresql://postgres.abcd:Pass123@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres"


@pytest.mark.parametrize("pasted", [
    GOOD,
    f'DATABASE_URL="{GOOD}"',             # whole .env line copied
    f"export DATABASE_URL={GOOD}",
    f'"{GOOD}"',                          # quotes
    f"  {GOOD}\n",                        # spaces / new line
    f"{GOOD}?pgbouncer=true&connection_limit=1",  # Prisma tab
])
def test_paste_accidents_are_cleaned_up(pasted):
    from app.db import check_url, normalize_url

    url = normalize_url(pasted)
    assert url == GOOD and check_url(url) is None


def test_jdbc_and_ssl_parameters_survive_cleanup():
    from app.db import check_url, normalize_url

    url = normalize_url("jdbc:postgresql://postgres.abcd:Pass123@host.pooler.supabase.com:5432/postgres?sslmode=require&pgbouncer=true")
    assert url == "postgresql://postgres.abcd:Pass123@host.pooler.supabase.com:5432/postgres?sslmode=require"
    assert check_url(url) is None


@pytest.mark.parametrize("pasted, expected", [
    ("psql -h aws-0-x.pooler.supabase.com -p 5432 -d postgres -U postgres.abcd", "ขึ้นต้นด้วย postgresql://"),
    ("User Id=postgres.abcd;Password=Pass123;Server=aws-0-x.pooler.supabase.com;Port=5432", "ขึ้นต้นด้วย postgresql://"),
    ("postgresql://aws-0-x.pooler.supabase.com:5432/postgres", "ไม่มีชื่อผู้ใช้และรหัสผ่าน"),
    ("postgresql://postgres.abcd:Pa/ss@aws-0-x.pooler.supabase.com:5432/postgres", "อักขระพิเศษ"),
    ("postgresql://postgres.abcd:100%sure@aws-0-x.pooler.supabase.com:5432/postgres", "อักขระพิเศษ"),
])
def test_unfixable_formats_are_named(pasted, expected):
    from app.db import check_url, normalize_url

    assert expected in check_url(normalize_url(pasted))


def test_key_value_conninfo_is_kept_as_is():
    from app.db import check_url, normalize_url

    url = "host=127.0.0.1 port=5432 dbname=postgres user=postgres"
    assert normalize_url(url) == url and check_url(url) is None
