"""Phone notifications: messages, recipients, preferences, Web Push and LINE."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.auth import LoginLimiter, MemoryUserStore, User
from app.notify import LineSender, MemoryNotifyStore, Message, Notifier, Target, build_messages, describe_change
from orchestra_engine import Assignment, ScheduleChange, SurgeryCase, Urgency, hm

SUB = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "BPk", "auth": "xyz"}}


def a(cid, start, room="OR-1", team=("S1", "N1")):
    return Assignment(cid, room, hm(start), hm(start) + 60, hm(start) + 90, tuple(team))


CASES = {"C1": SurgeryCase("C1", "HN-SECRET", "Lap chole", "S1", 60, patient_name="นาย ลับมาก"),
         "E1": SurgeryCase("E1", "HN-ER", "Laparotomy", "S2", 60, urgency=Urgency.EMERGENCY)}


class FakeAccounts:
    def __init__(self, users):
        self.store = type("S", (), {"list": lambda _self: users})()


USERS = [User("u-s1", "s1", "Surgeon 1", "surgeon", "S1"), User("u-n1", "n1", "Nurse 1", "staff", "N1"),
         User("u-n2", "n2", "Nurse 2", "staff", "N2"), User("u-sch", "sch", "Scheduler", "scheduler"),
         User("u-cssd", "cssd", "CSSD", "cssd")]


class FakePush:
    def __init__(self, result="sent"):
        self.sent, self.result = [], result

    def public_key(self):
        return "PUBKEY"

    def send(self, target, payload):
        self.sent.append((target.address, payload))
        return self.result, None


def notifier(push=None, line=None):
    return Notifier(FakeAccounts(USERS), MemoryNotifyStore(), push or FakePush(), line or LineSender("", "", ""),
                    background=False)


def test_change_lines_never_carry_patient_identity():
    moved = ScheduleChange("C1", [], a("C1", "09:00"), a("C1", "10:30", "OR-2"))
    assert describe_change(moved, "N1", CASES) == "C1: ย้ายเป็น 10:30-11:30 ห้อง OR-2 (เดิม 09:00 ห้อง OR-1)"
    new = ScheduleChange("E1", [], None, a("E1", "11:00", team=("S2", "N2")))
    assert describe_change(new, "N2", CASES) == "E1 (ฉุกเฉิน): เคสใหม่ 11:00-12:00 ห้อง OR-1"
    gone = ScheduleChange("C1", [], a("C1", "09:00"), None)
    assert describe_change(gone, "S1", CASES) == "C1: ถูกเอาออกจากตาราง (รอจัดวันใหม่)"
    swap = ScheduleChange("C1", [], a("C1", "09:00"), a("C1", "09:00", team=("S1", "N2")))
    assert describe_change(swap, "N1", CASES).startswith("C1: คุณไม่อยู่ในทีมแล้ว")
    assert describe_change(swap, "N2", CASES).startswith("C1: คุณถูกจัดเข้าทีม")
    assert describe_change(swap, "S1", CASES) is None  # nothing changed for the surgeon

    event = {"type": "schedule", "cause": "emergency", "day": "2026-11-02", "changes": [moved, new, gone], "cases": CASES,
             "unscheduled": ["C9"], "new_case": "E1", "fatigue": 2, "actor_id": "u-s1"}
    messages = build_messages(event, USERS)
    text = json.dumps([m.__dict__ for m in messages], ensure_ascii=False)
    assert "HN-" not in text and "ลับมาก" not in text
    who = {(m.user_id, m.kind) for m in messages}
    assert who == {("u-n1", "case"), ("u-n2", "case"), ("u-sch", "emergency")}  # the actor (u-s1) is skipped
    summary = next(m for m in messages if m.kind == "emergency")
    assert summary.lines == ["E1: เข้า 11:00-12:00 ห้อง OR-1", "ย้าย/เปลี่ยนทีม 1 เคส", "รอจัดวันใหม่ 1 เคส",
                             "บุคลากรเกินเกณฑ์ความเหนื่อยล้า 2 คน"]
    assert summary.url == "/?date=2026-11-02"


def test_long_messages_are_cut():
    m = Message("u", "case", "t", [f"line {i}" for i in range(9)])
    assert m.body.splitlines()[-1] == "และอีก 3 รายการ"


def test_preferences_and_delivery_log():
    push = FakePush()
    n = notifier(push)
    for user in USERS:
        n.subscribe(user, {**SUB, "endpoint": SUB["endpoint"] + user.user_id}, "Chrome")
    event = {"type": "cssd", "day": "2026-11-02", "unit": "DV-01", "status": "STERILIZING", "ready": "13:00",
             "cases": ["R-1"], "actor_id": "u-cssd"}
    n.handle(event)
    assert sorted(addr[-5:] for addr, _ in push.sent) == ["u-sch"]  # CSSD (actor) skipped; staff not subscribed to cssd
    n.set_prefs(USERS[3], {"cssd": False})
    push.sent.clear()
    n.handle(event)
    assert push.sent == []
    assert n.store.recent("u-sch")[0]["title"] == "CSSD: DV-01 ไม่ทันเคสที่จองไว้"
    assert n.status(USERS[3])["prefs"] == {"case": True, "emergency": True, "cssd": False, "readiness": True,
                                           "feedback": False}
    assert "feedback" not in n.status(USERS[3])["kinds"] and "emergency" not in n.status(USERS[1])["kinds"]


def test_expired_push_subscription_is_removed():
    n = notifier(FakePush("removed"))
    n.subscribe(USERS[1], SUB, "Phone")
    assert n.test(USERS[1]) == ["removed"]
    assert n.store.targets("u-n1") == []
    with pytest.raises(ValueError):
        n.test(USERS[1])  # nothing left to send to
    with pytest.raises(ValueError):
        n.subscribe(USERS[1], {"endpoint": "http://insecure", "keys": {}}, None)


class FakeLine(LineSender):
    def __init__(self):
        super().__init__("token", "secret", "https://lin.ee/abc")
        self.posts = []

    def _post(self, path, payload):
        self.posts.append((path, payload))
        return "sent", None


def sign(body: bytes, secret="secret") -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()


def test_line_link_and_push():
    line = FakeLine()
    n = notifier(line=line)
    code = n.link_code(USERS[1])
    body = json.dumps({"events": [{"type": "message", "replyToken": "r1", "source": {"userId": "U123"},
                                   "message": {"type": "text", "text": f"รหัส {code}"}}]}).encode()
    with pytest.raises(PermissionError):
        n.line_webhook(body, "forged")
    assert n.line_webhook(body, sign(body)) == 1
    assert n.status(USERS[1])["line"]["linked"]
    assert "Nurse 1" in line.posts[-1][1]["messages"][0]["text"]
    assert n.line_webhook(body, sign(body)) == 0  # a code works once
    assert "รหัสไม่ถูกต้อง" in line.posts[-1][1]["messages"][0]["text"]

    n.deliver(Message("u-n1", "case", "ตารางผ่าตัดของคุณเปลี่ยน", ["C1: ย้าย"], "/?date=2026-11-02"))
    path, payload = line.posts[-1]
    assert path == "push" and payload["to"] == "U123" and "C1: ย้าย" in payload["messages"][0]["text"]
    unfollow = json.dumps({"events": [{"type": "unfollow", "source": {"userId": "U123"}}]}).encode()
    n.line_webhook(unfollow, sign(unfollow))
    assert not n.status(USERS[1])["line"]["linked"]


# ------------------------------------------------------------------- through the API (demo mode)

@pytest.fixture()
def client(monkeypatch):
    from app import main

    monkeypatch.setenv("APP_PASSWORD", "owner-secret-123")
    main.store.reset()
    main.accounts.store = MemoryUserStore()
    main.accounts.limiter = LoginLimiter()
    push = FakePush()
    main.notifier.store = MemoryNotifyStore()
    main.notifier.webpush = push
    main.notifier.background = False
    main.notifier._pool = None
    c = TestClient(main.app, base_url="https://testserver")
    assert c.post("/api/login", json={"username": "orchestra", "password": "owner-secret-123"}).status_code == 200
    return c, main, push


def test_notifications_api_and_emergency(client):
    c, main, push = client
    status = c.get("/api/notify").json()
    assert status["ready"] and status["webpush"]["public_key"] == "PUBKEY" and not status["line"]["enabled"]
    assert c.post("/api/notify/test", json={}).status_code == 400  # no device yet
    c.post("/api/notify/subscribe", json={"subscription": SUB, "label": "Chrome on Android"})
    r = c.post("/api/notify/test", json={}).json()
    assert r["sent"] == 1 and push.sent[-1][1]["title"] == "ทดสอบการแจ้งเตือน ORchestraAI"
    assert c.post("/api/notify/line-code", json={}).status_code == 400  # LINE not configured
    for path in ("/sw.js", "/manifest.webmanifest", "/icons/icon-192.png"):
        assert TestClient(main.app).get(path).status_code == 200  # public, before login
    assert TestClient(main.app).post("/api/line/webhook", content=b"{}",
                                     headers={"content-type": "application/json"}).status_code == 403

    # The on-call surgeon's account gets a push when an emergency is inserted (the owner, as actor, does not).
    temp = "Temporary-Pass-77"
    c.post("/api/users", json={"username": "anan", "display_name": "Dr.Anan", "role": "surgeon",
                               "staff_id": "SUR-ONCALL", "password": temp})
    nurse = TestClient(main.app, base_url="https://testserver")
    nurse.post("/api/login", json={"username": "anan", "password": temp})
    nurse.post("/api/password", json={"current_password": temp, "new_password": "Real-Pass-2026!"})
    nurse.post("/api/notify/subscribe", json={"subscription": {**SUB, "endpoint": SUB["endpoint"] + "-nurse"}})
    push.sent.clear()
    body = {"case": {"procedure": "Exploratory laparotomy", "patient_hn": "HN-9999", "surgeon_id": "SUR-ONCALL",
                     "duration_mins": 120, "required_equipment_types": ["MAJOR_GENERAL_TRAY"]}, "now": "10:30"}
    assert c.post("/api/emergency", json=body).status_code == 200
    to_nurse = [p for addr, p in push.sent if addr.endswith("-nurse")]
    assert to_nurse and "HN-9999" not in json.dumps(push.sent, ensure_ascii=False)
    assert "ฉุกเฉิน" in to_nurse[0]["body"]
    assert any(p["tag"] == "emergency" for addr, p in push.sent if addr == SUB["endpoint"]) is False  # owner was the actor


def test_web_push_sender_keys_and_expiry(monkeypatch):
    import pywebpush

    from app.notify import WebPushSender

    store = MemoryNotifyStore()
    sender = WebPushSender(store, "mailto:ops@example.org")
    key = sender.public_key()
    assert len(base64.urlsafe_b64decode(key + "==")) == 65  # uncompressed P-256 point for applicationServerKey
    assert WebPushSender(store).public_key() == key  # made once, kept in settings
    calls = []
    monkeypatch.setattr(pywebpush, "webpush", lambda sub, data, **kw: calls.append((sub, json.loads(data), kw)))
    target = Target("u", "webpush", SUB["endpoint"], SUB["keys"])
    assert sender.send(target, {"title": "t", "body": "b"}) == ("sent", None)
    assert calls[0][0]["endpoint"] == SUB["endpoint"] and calls[0][2]["vapid_claims"] == {"sub": "mailto:ops@example.org"}

    class Gone:
        status_code = 410

    def expired(*args, **kwargs):
        raise pywebpush.WebPushException("gone", response=Gone())

    monkeypatch.setattr(pywebpush, "webpush", expired)
    assert sender.send(target, {})[0] == "removed"


def test_web_push_is_encrypted_for_the_device(monkeypatch):
    """Real VAPID signing and payload encryption, sent to a local stand-in push service and
    decrypted with the device's keys (what a phone's browser does)."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import http_ece
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    from app.notify import WebPushSender

    received = {}

    class PushService(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            received["headers"] = {k.lower(): v for k, v in self.headers.items()}
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(201)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), PushService)
    threading.Thread(target=server.handle_request, daemon=True).start()
    device = ec.generate_private_key(ec.SECP256R1())
    public = device.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = b"0123456789abcdef"
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()  # noqa: E731
    monkeypatch.setattr("app.notify.push_endpoint_ok", lambda endpoint: True)  # a local stand-in push service
    target = Target("u", "webpush", f"http://127.0.0.1:{server.server_port}/push/abc",
                    {"p256dh": b64(public), "auth": b64(auth)})
    payload = {"title": "ตารางผ่าตัดของคุณเปลี่ยน", "body": "C1: ย้ายเป็น 10:30", "url": "/", "tag": "case"}
    assert WebPushSender(MemoryNotifyStore(), "mailto:ops@example.org").send(target, payload) == ("sent", None)
    server.server_close()
    assert received["headers"]["authorization"].startswith("vapid t=")  # RFC 8292 VAPID, signed by the server
    assert received["headers"]["content-encoding"] == "aes128gcm"
    plain = http_ece.decrypt(received["body"], private_key=device, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain) == payload
