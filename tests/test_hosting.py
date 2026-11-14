"""Where the server runs (Railway, Render, elsewhere) and an old server after a move (APP_MOVED_TO)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import hosting
from app.auth import db_down
from app.main import accounts, app
from test_app import OWNER, fresh, owner, session  # noqa: F401  (fixtures)

HOST_VARS = ("RAILWAY_PROJECT_ID", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_PUBLIC_DOMAIN",
             "RAILWAY_GIT_COMMIT_SHA", "RAILWAY_REPLICA_REGION", "RENDER", "RENDER_SERVICE_ID", "RENDER_EXTERNAL_URL",
             "RENDER_GIT_COMMIT", "APP_URL", "APP_MOVED_TO", "CLIENT_IP_HEADER")
NEW = "https://orchestra.up.railway.app"


@pytest.fixture(autouse=True)
def nowhere(monkeypatch):
    for name in HOST_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def railway(monkeypatch):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p-1")
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "orchestra.up.railway.app")
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "abc1234def5678")
    monkeypatch.setenv("RAILWAY_REPLICA_REGION", "asia-southeast1-eqsg3a")


def test_which_host(monkeypatch, railway):
    assert hosting.provider() == "railway" and hosting.name() == "Railway"
    assert hosting.public_url() == NEW and hosting.commit() == "abc1234def5678"
    assert "Variables" in hosting.settings_place() and "Railway" in db_down()
    monkeypatch.setenv("APP_URL", "https://or.hospital.example/")  # a custom domain wins
    assert hosting.public_url() == "https://or.hospital.example"
    for name in ("RAILWAY_PROJECT_ID", "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_GIT_COMMIT_SHA", "APP_URL"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://orchestra-ai.onrender.com")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "fff0001")
    assert hosting.provider() == "render" and hosting.settings_place() == "Render → Environment"
    assert hosting.public_url() == "https://orchestra-ai.onrender.com" and hosting.commit() == "fff0001"
    monkeypatch.delenv("RENDER")
    assert hosting.provider() == "" and "เซิร์ฟเวอร์" in hosting.settings_place()


def test_client_ip_comes_only_from_the_hosts_own_header(monkeypatch, railway):
    sent = {"x-real-ip": "203.0.113.7", "cf-connecting-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"}
    assert hosting.client_ip(sent, "10.0.0.2") == "203.0.113.7"  # Railway: a caller's CF-Connecting-IP is ignored
    assert hosting.client_ip({"cf-connecting-ip": "1.2.3.4"}, "10.0.0.2") == "10.0.0.2"
    monkeypatch.delenv("RAILWAY_PROJECT_ID")
    monkeypatch.setenv("RENDER", "true")
    assert hosting.client_ip(sent, "10.0.0.2") == "1.2.3.4"  # Render: Cloudflare's header
    assert hosting.client_ip({"x-forwarded-for": "5.6.7.8, 10.1.1.1"}, None) == "5.6.7.8"
    monkeypatch.setenv("CLIENT_IP_HEADER", "X-Real-IP")  # a hospital's own nginx
    assert hosting.client_ip(sent, "10.0.0.2") == "203.0.113.7"
    monkeypatch.setenv("CLIENT_IP_HEADER", "none")
    assert hosting.client_ip(sent, "10.0.0.2") == "10.0.0.2"


def test_login_limits_on_railway_cannot_be_dodged_with_a_forged_header(railway):
    c = TestClient(app, base_url="https://testserver")
    for i in range(5):
        r = c.post("/api/login", json={"username": "nobody", "password": "wrong"},
                   headers={"X-Real-IP": "203.0.113.7", "CF-Connecting-IP": f"198.51.100.{i}"})
        assert r.status_code == 401
    blocked = c.post("/api/login", json={"username": "nobody", "password": "wrong"},
                     headers={"X-Real-IP": "203.0.113.7", "CF-Connecting-IP": "198.51.100.99"})
    assert blocked.status_code == 429
    other = c.post("/api/login", json={"username": "nobody", "password": "wrong"}, headers={"X-Real-IP": "203.0.113.8"})
    assert other.status_code == 401


def test_status_and_security_pages_name_the_host(railway, owner):
    status = owner.get("/api/system/status").json()
    assert status["version"] == "abc1234" and status["host"]["name"] == "Railway"
    row = next(s for s in status["settings"] if s["label"] == "เซิร์ฟเวอร์ที่ใช้")
    assert "asia-southeast1" in row["value"] and "Variables" in row["value"]
    assert next(s for s in status["settings"] if s["key"] == "APP_URL")["value"] == NEW
    assert owner.get("/api/2fa").json()["host"]["settings"].startswith("Railway")
    r = owner.post("/api/password", json={"current_password": OWNER[1], "new_password": "Another-Pass-2026"})
    assert r.status_code == 400 and "Railway" in r.json()["detail"]


# ------------------------------------------------------------------ an old server after a move

def test_moved_to_reads_only_an_https_address(monkeypatch):
    for raw, want in (("https://orchestra.up.railway.app/login?x=1", NEW), ("orchestra.up.railway.app", NEW),
                      ("http://orchestra.up.railway.app", ""), ("https://user:pw@evil.example", ""), ("", ""),
                      ("javascript:alert(1)", "")):
        monkeypatch.setenv("APP_MOVED_TO", raw)
        assert hosting.moved_to() == want, raw


def test_an_old_server_sends_everyone_to_the_new_address(monkeypatch):
    monkeypatch.setenv("APP_MOVED_TO", NEW)
    c = TestClient(app, base_url="https://old.onrender.com")
    page = c.get("/board", follow_redirects=False)
    assert page.status_code == 200 and "ย้ายไปที่อยู่ใหม่แล้ว" in page.text
    assert f'href="{NEW}/board"' in page.text and "unregister" in page.text  # the old notification worker goes
    assert page.headers["content-security-policy"] and page.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" in page.headers
    for path in ("/api/state", "/api/his/v1/schedule"):
        r = c.get(path)
        assert r.status_code == 410 and r.json()["moved_to"] == NEW
    assert c.post("/api/login", json={"username": OWNER[0], "password": OWNER[1]}).status_code == 410
    assert c.get("/api/health").status_code == 200  # the host's health check
    assert c.get("/sw.js").status_code == 200 and c.get("/icons/icon-192.png").status_code == 200
    odd = c.get('/"><script>x</script>')
    assert "<script>x</script>" not in odd.text
    # APP_MOVED_TO copied to the new server by mistake: it keeps working instead of sending people to itself
    here = TestClient(app, base_url=NEW)
    assert "ย้ายไปที่อยู่ใหม่แล้ว" not in here.get("/login").text


def test_an_old_server_runs_no_scheduled_jobs(monkeypatch):
    started = []

    class Timer:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            started.append(self)

        def stop(self):
            pass

    monkeypatch.setattr(main, "DigestTimer", Timer)
    with TestClient(app):
        pass
    assert len(started) == 1  # the live server: the daily reminder (demo mode)
    monkeypatch.setenv("APP_MOVED_TO", NEW)
    with TestClient(app):
        pass
    assert len(started) == 1


def test_accounts_untouched(owner):
    assert accounts.store.list() == []  # nothing above created users
