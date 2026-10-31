from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from orchestra_engine.intake import (
    DurationPredictor,
    HistoryRecord,
    mask_identifiers,
    parse_rules,
    synthetic_history,
    triage,
)

TODAY = date(2026, 10, 31)  # a Saturday
PROCS = [
    {"id": "p-chole", "name": "Laparoscopic cholecystectomy", "duration": 75},
    {"id": "p-prost", "name": "Robotic radical prostatectomy", "duration": 210},
    {"id": "p-tka", "name": "Total knee arthroplasty (L)", "duration": 150},
    {"id": "p-lap", "name": "Emergency exploratory laparotomy", "duration": 120},
    {"id": "p-appy", "name": "Laparoscopic appendectomy", "duration": 60},
]
SURGEONS = [{"id": "s-napat", "name": "Dr.Napat (General)"}, {"id": "s-kit", "name": "Dr.Kittipong (Uro)"},
            {"id": "s-som", "name": "นพ.สมชาย ใจดี"}]


# ------------------------------------------------------------- prediction

def _predictor(history, cards=None):
    procs = {p["id"]: p for p in PROCS}
    cards = cards if cards is not None else {("s-napat", "p-chole"): {"duration": 75, "stddev": 15}}
    return DurationPredictor(procs, cards, history, {"s-napat": "Dr.Napat"})


def test_no_history_uses_the_preference_card():
    p = _predictor([]).predict("s-napat", "p-chole")
    assert p.expected_mins == 75 and p.basis == "card" and p.history_same == 0
    assert p.recommended_mins >= p.expected_mins and p.recommended_mins % 5 == 0


def test_own_history_pulls_the_estimate_towards_reality():
    history = [HistoryRecord("s-napat", "p-chole", m) for m in (95, 100, 105, 98, 102, 100)]
    p = _predictor(history).predict("s-napat", "p-chole")
    # (3 * 75 + 6 * 100) / 9 = 91.7
    assert p.expected_mins == 92 and p.history_same == 6
    assert any("6 เคส" in line for line in p.explanation)


def test_speed_on_other_procedures_is_used_when_this_one_is_new():
    history = [HistoryRecord("s-napat", "p-appy", 72) for _ in range(9)]  # 1.2x the 60-min standard
    p = _predictor(history).predict("s-napat", "p-chole")
    # factor = (3 + 9 * 1.2) / 12 = 1.15 -> 75 * 1.15 = 86
    assert p.expected_mins == 86 and p.history_other == 9 and p.history_same == 0


def test_complexity_scales_and_outliers_are_ignored():
    history = [HistoryRecord("s-napat", "p-chole", 75), HistoryRecord("s-napat", "p-chole", 900)]  # 900 = typo
    pred = _predictor(history)
    assert pred.history_count == 1
    assert pred.predict("s-napat", "p-chole", "complex").expected_mins == round(75 * 1.3)
    assert pred.predict("s-napat", "p-chole", "simple").expected_mins == round(75 * 0.85)


def test_procedure_standard_time_when_no_card():
    p = _predictor([], cards={}).predict("s-napat", "p-tka")
    assert p.basis == "procedure" and p.expected_mins == 150


def test_synthetic_history_is_reproducible():
    cards = [{"surgeon_id": "s-napat", "procedure_id": "p-chole", "duration": 75}]
    assert synthetic_history(cards) == synthetic_history(cards)
    assert all(r["days_ago"] >= 2 for r in synthetic_history(cards))


# ----------------------------------------------------------------- rules

def test_thai_request_is_read():
    f = parse_rules("หมอ Napat ขอผ่าตัดถุงน้ำดีส่องกล้อง HN 123456 พรุ่งนี้ 9 โมง เคสซับซ้อน มีพังผืด",
                    PROCS, SURGEONS, TODAY)
    assert (f["procedure_id"], f["surgeon_id"], f["date"], f["start"], f["complexity"], f["patient_hn"]) == (
        "p-chole", "s-napat", "2026-11-01", "09:00", "complex", "123456")
    assert f["urgency"] == "ELECTIVE"


def test_english_request_with_buddhist_year_and_negation():
    f = parse_rules("Dr.Kittipong robotic prostatectomy HN-0042 on 3/11/2569 at 13:30, not urgent",
                    PROCS, SURGEONS, TODAY)
    assert f["procedure_id"] == "p-prost" and f["date"] == "2026-11-03" and f["start"] == "13:30"
    assert f["urgency"] == "ELECTIVE"  # "stat" inside "prostatectomy" and "not urgent" must not escalate


@pytest.mark.parametrize("text, urgency", [
    ("ผู้ป่วยลำไส้ทะลุ ต้องเปิดหน้าท้องด่วนมาก", "EMERGENCY"),
    ("ruptured AAA, shock", "EMERGENCY"),
    ("ไส้ติ่งอักเสบ ขอผ่าตัดด่วน ภายใน 24 ชม", "URGENT"),
    ("เคสไม่ด่วน นัดสัปดาห์หน้า", "ELECTIVE"),
    ("อยากผ่าตัดเปลี่ยนข้อเข่า", "ELECTIVE"),
])
def test_triage(text, urgency):
    assert triage(text)[0] == urgency


def test_times_dates_and_durations():
    f = parse_rules("นพ.สมชาย เปลี่ยนข้อเข่า บ่ายสองโมง วันศุกร์ ใช้เวลา 2.5 ชั่วโมง ไม่ซับซ้อน", PROCS, SURGEONS, TODAY)
    assert (f["surgeon_id"], f["procedure_id"], f["start"], f["date"], f["duration_mins"], f["complexity"]) == (
        "s-som", "p-tka", "14:00", "2026-11-06", 150, "simple")
    assert parse_rules("ไส้ติ่ง ภายใน 24 ชม", PROCS, SURGEONS, TODAY)["duration_mins"] is None  # deadline, not length


def test_identifiers_are_masked():
    masked, hn = mask_identifiers("HN 12345/66 โทร 0812345678 ผ่าพรุ่งนี้")
    assert hn == "12345/66" and "12345" not in masked and "0812345678" not in masked


# ------------------------------------------------------------ web app

OWNER = ("orchestra", "owner-secret-123")


@pytest.fixture()
def app_client(monkeypatch):
    from app.auth import LoginLimiter, MemoryUserStore
    from app.main import accounts, app, intake, store

    monkeypatch.setenv("APP_PASSWORD", OWNER[1])
    monkeypatch.delenv("APP_USERNAME", raising=False)
    store.reset()
    accounts.store = MemoryUserStore()
    accounts.limiter = LoginLimiter()
    monkeypatch.setattr(intake.llm, "api_key", None)  # rules only unless a test fakes Claude
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/api/login", json={"username": OWNER[0], "password": OWNER[1]}).status_code == 200
    return c


def _user(owner, username, role, staff_id=None):
    from app.main import app

    owner.post("/api/users", json={"username": username, "display_name": username, "role": role,
                                   "staff_id": staff_id, "password": "Temporary-Pass-77"})
    c = TestClient(app, base_url="https://testserver")
    c.post("/api/login", json={"username": username, "password": "Temporary-Pass-77"})
    c.post("/api/password", json={"current_password": "Temporary-Pass-77", "new_password": "Real-Pass-2026!"})
    return c


def test_parse_fills_the_form_with_a_forecast(app_client):
    r = app_client.post("/api/intake/parse", json={"text": "หมอ Napat ผ่าถุงน้ำดีส่องกล้อง HN 777 พรุ่งนี้ 9 โมง ซับซ้อน"})
    assert r.status_code == 200
    body = r.json()
    f = body["fields"]
    assert body["source"] == "rules"
    assert f["surgeon_id"] == "SUR-GEN" and f["procedure_id"] == "P-laparoscopic-cholecystectomy"
    assert f["required_equipment_types"] == ["LAPAROSCOPY_TRAY"]
    assert f["duration_mins"] == body["prediction"]["recommended_mins"] and f["duration_source"] == "พยากรณ์จากประวัติ"
    assert body["prediction"]["history_same"] > 0  # demo history


def test_surgeon_parse_is_pinned_to_their_own_name(app_client):
    doc = _user(app_client, "napat", "surgeon", "SUR-GEN")
    f = doc.post("/api/intake/parse", json={"text": "Dr.Kittipong robotic prostatectomy tomorrow"}).json()
    assert f["fields"]["surgeon_id"] == "SUR-GEN"
    assert any("เฉพาะเคสของตัวเอง" in w for w in f["warnings"])
    assert _user(app_client, "cssd1", "cssd").post("/api/intake/parse", json={"text": "test text"}).status_code == 403


def test_recorded_actual_times_feed_the_forecast(app_client):
    pid = "P-laparoscopic-cholecystectomy"
    before = app_client.post("/api/intake/predict", json={"surgeon_id": "SUR-GEN", "procedure_id": pid}).json()
    count = app_client.get("/api/intake/info").json()["history_count"]
    from app.main import store

    case_id = next(cid for cid, c in store.result.cases.items() if c.procedure == "Laparoscopic cholecystectomy")
    r = app_client.post(f"/api/cases/{case_id}/actual", json={"start": "08:00", "end": "10:30"})
    assert r.status_code == 200 and r.json()["result"]["actual_minutes"] == 150
    booking = next(b for b in r.json()["state"]["schedule"]["bookings"] if b["case_id"] == case_id)
    assert (booking["actual_start"], booking["actual_end"]) == ("08:00", "10:30")
    assert app_client.get("/api/intake/info").json()["history_count"] == count + 1
    after = app_client.post("/api/intake/predict", json={"surgeon_id": "SUR-GEN", "procedure_id": pid}).json()
    assert after["prediction"]["expected_mins"] > before["prediction"]["expected_mins"]


def test_only_the_team_records_times(app_client):
    from app.main import store

    a = next(iter(store.result.assignments.values()))
    on_team, off_team = a.staff_ids[1], next(s.staff_id for s in store.hospital.staff
                                              if s.role == "SCRUB_NURSE" and s.staff_id not in a.staff_ids)
    assert _user(app_client, "onteam", "staff", on_team).post(
        f"/api/cases/{a.case_id}/actual", json={"start": "08:05"}).status_code == 200
    assert _user(app_client, "offteam", "staff", off_team).post(
        f"/api/cases/{a.case_id}/actual", json={"start": "08:05"}).status_code == 403
    assert _user(app_client, "cssd2", "cssd").post(
        f"/api/cases/{a.case_id}/actual", json={"start": "08:05"}).status_code == 403
    assert app_client.post(f"/api/cases/{a.case_id}/actual", json={"end": "09:00"}).status_code == 200


class FakeLLM:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.seen = answer, error, None
        self.enabled, self.model = True, "fake"

    def parse(self, masked, procedures, surgeons, today):
        self.seen = masked
        if self.error:
            raise self.error
        return self.answer


def test_claude_answer_is_used_but_never_downgrades_urgency(app_client, monkeypatch):
    from app.main import intake

    fake = FakeLLM({"procedure_id": "P-emergency-exploratory-laparotomy", "surgeon_id": "SUR-ONCALL",
                    "urgency": "ELECTIVE", "urgency_reason": "ok", "complexity": "standard"})
    monkeypatch.setattr(intake, "llm", fake)
    body = app_client.post("/api/intake/parse", json={"text": "HN 4455 ลำไส้ทะลุ ขอเปิดหน้าท้อง"}).json()
    assert body["source"] == "claude" and "4455" not in fake.seen
    assert body["fields"]["urgency"] == "EMERGENCY"  # keyword safety net wins
    assert body["fields"]["patient_hn"] == "4455"  # read locally, never sent


def test_claude_failure_falls_back_to_rules(app_client, monkeypatch):
    from app.intake_llm import IntakeUnavailable
    from app.main import intake

    monkeypatch.setattr(intake, "llm", FakeLLM(error=IntakeUnavailable("down")))
    body = app_client.post("/api/intake/parse", json={"text": "หมอ Napat ผ่าไส้ติ่ง พรุ่งนี้ 10:00"}).json()
    assert body["source"] == "rules" and any("down" in w for w in body["warnings"])
    assert body["fields"]["procedure_id"] == "P-laparoscopic-appendectomy"


# --------------------------------------------------------- Claude request

def test_claude_request_is_masked_coded_and_schema_bound(monkeypatch):
    from app.intake_llm import ClaudeIntake

    captured = {}

    def create(**kwargs):
        captured.update(kwargs)
        answer = {"procedure_code": "P1", "procedure_text": "", "surgeon_code": "S2", "urgency": "URGENT",
                  "urgency_reason": "ด่วน", "date": "2026-11-01", "start_time": "09:00", "duration_mins": 0,
                  "complexity": "complex", "requires_icu": False, "infection_risk": "CLEAN", "notes": ""}
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(answer))])

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    llm = ClaudeIntake(api_key="test-key")
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    masked, _ = mask_identifiers("HN 99887 Kittipong lap chole tomorrow 9am complex")
    out = llm.parse(masked, PROCS, SURGEONS, TODAY)

    assert out["procedure_id"] == "p-chole" and out["surgeon_id"] == "s-kit" and out["duration_mins"] is None
    sent = captured["messages"][0]["content"]
    assert "99887" not in sent and "p-chole" not in sent and "P1: Laparoscopic cholecystectomy" in sent
    assert captured["model"] == "claude-opus-5-5"
    assert captured["output_config"]["format"]["type"] == "json_schema"
    assert captured["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert captured["fallbacks"] == "default" and captured["betas"] == ["server-side-fallback-2026-07-01"]


def test_claude_refusal_is_reported(monkeypatch):
    from app.intake_llm import ClaudeIntake, IntakeUnavailable

    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(
        create=lambda **kw: SimpleNamespace(stop_reason="refusal", content=[]))))
    llm = ClaudeIntake(api_key="test-key")
    monkeypatch.setattr(llm, "_get_client", lambda: client)
    with pytest.raises(IntakeUnavailable):
        llm.parse("text", PROCS, SURGEONS, TODAY)
