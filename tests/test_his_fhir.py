"""Master data from the HIS in HL7 FHIR R4: operating rooms (Location), OR staff (Practitioner +
PractitionerRole), OR equipment (Device) and rosters (Schedule + Slot); what the HIS owns is locked here.

The database tests run against a real PostgreSQL server (TEST_DATABASE_URL).
"""
from __future__ import annotations

import io
import json
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app import his as hm
from app import his_fhir as hf
from app.admin_store import Inventory
from app.auth import DbUserStore
from app.main import accounts
from app.store import StoreError
from fhir_sample import device, location, practitioner, role, sample, schedule, slot
from fhir_sample import bundle as make_bundle
from test_app import OWNER, fresh, make_user, owner, session  # noqa: F401  (fixtures)
from test_db import ADMIN_URL, _rows, db_url, store  # noqa: F401  (fixtures)

needs_db = pytest.mark.skipif(not ADMIN_URL, reason="set TEST_DATABASE_URL to run database tests")
ADMIN = {"name": "Admin", "role": "admin"}


def _settings(**extra):
    return {**json.loads(json.dumps(hf.DEFAULTS)), **extra}


# ------------------------------------------------------------------ mapping (no database)

def test_locations_rooms_and_what_is_not_an_or():
    s = _settings()
    assert hf.map_location(location("a", "OR-7", "ห้อง 7", "OR"), s) == \
        {"code": "OR-7", "type": "STANDARD", "status": "AVAILABLE", "name": "ห้อง 7"}
    assert hf.map_location(location("b", "OR-8", "ความดันลบ", "OR-NEG", status="inactive"), s)["status"] == "CLOSED"
    assert hf.map_location(location("c", "OR-9", "ห้อง 9", "OR", status="suspended"), s)["status"] == "MAINTENANCE"
    with pytest.raises(hf.Skip, match="ไม่ใช่ห้องผ่าตัด"):
        hf.map_location(location("w", "W-1", "หอผู้ป่วย", "WARD"), s)
    with pytest.raises(hf.Skip, match="เตียง"):
        hf.map_location(location("d", "B-1", "เตียง 1", "OR", physical="bd"), s)
    with pytest.raises(StoreError, match="รหัสห้อง"):
        hf.map_location(location("e", "ห้อง ผ่าตัด ใหญ่", "x", "OR"), s)
    named = hf.map_location({**location("f", None, "OR-10", "OR"), "identifier": []}, s)
    assert named["code"] == "OR-10"  # no identifier: the name


def test_practitioners_by_role_and_names():
    s = _settings()
    p = practitioner("p1", "D-1", "นพ.ก ข", license_no="ว.1")
    assert hf.map_practitioner(p, [role("r1", "p1", "SURGEON", "Ortho")], s) == \
        {"code": "D-1", "name": "นพ.ก ข", "role": "SURGEON", "active": True, "specialty": "Ortho"}
    p2 = practitioner("p2", "N-2", None, prefix="พย.", given="สมใจ", family="ดีงาม")
    assert hf.map_practitioner(p2, [role("r2", "p2", "SCRUB_NURSE")], s)["name"] == "พย.สมใจ ดีงาม"
    with pytest.raises(hf.Skip):  # a pharmacist
        hf.map_practitioner(practitioner("p3", "P-3", "ภก.ค"), [role("r3", "p3", "PHARMACIST")], s)
    with pytest.raises(hf.Skip):  # the OR role ended
        hf.map_practitioner(practitioner("p4", "D-4", "นพ.ง"), [role("r4", "p4", "SURGEON", active=False)], s)
    mapped = _settings(roles={"0055": "ANESTHESIOLOGIST"})  # the hospital's own code, by qualification
    q = {**practitioner("p5", "D-5", "พญ.จ"), "qualification": [{"code": {"coding": [{"code": "0055"}]}}]}
    assert hf.map_practitioner(q, [], mapped)["role"] == "ANESTHESIOLOGIST"
    no_code = {**practitioner("p6", "x", "นพ.ฉ"), "identifier": [{"value": "ว.66", "type": {"coding": [{"code": "PRN"}]}}]}
    assert hf.map_practitioner(no_code, [role("r6", "p6", "SURGEON")], s)["code"] == "ว.66"  # license when no EI
    assert hf.map_practitioner({**p, "active": False}, [role("r1", "p1", "SURGEON")], s)["active"] is False


def test_devices_by_type():
    types = {"laparoscopy_tray": "LAPAROSCOPY_TRAY", "c_arm": "C_ARM"}
    s = _settings(equipment_types={"6515-01": "C_ARM", "6515-99": "MISSING"})
    assert hf.map_device(device("d1", "LAP-9", "LAPAROSCOPY_TRAY", where="x"), s, types) == \
        {"serial": "LAP-9", "type": "LAPAROSCOPY_TRAY", "retired": False, "location": "Location/x", "location_text": None}
    assert hf.map_device(device("d2", "CA-9", "6515-01"), s, types)["type"] == "C_ARM"
    assert hf.map_device(device("d3", "CA-8", "C_ARM", status="inactive"), s, types)["retired"] is True
    with pytest.raises(hf.Skip):
        hf.map_device(device("d4", "BED-1", "HOSPITAL_BED"), s, types)
    with pytest.raises(StoreError, match="MISSING"):
        hf.map_device(device("d5", "X-1", "6515-99"), s, types)


def test_slots_are_shifts():
    from zoneinfo import ZoneInfo

    from datetime import date

    tz, s, day = ZoneInfo("Asia/Bangkok"), _settings(), date(2026, 12, 1)
    kind, start, end, note = hf.map_slot(slot("a", "s", day, "08:00", "16:00"), s, tz)
    assert (kind, (end - start).seconds // 3600, note) == ("REGULAR", 8, None)
    assert hf.map_slot(slot("b", "s", day, "16:00", "08:00", kind="ON_CALL"), s, tz)[0] == "ON_CALL"
    assert hf.map_slot(slot("c", "s", day, "08:00", "16:00", status="busy-unavailable", comment="ลาป่วย"), s, tz)[::3] == \
        ("LEAVE", "ลาป่วย")
    assert hf.map_slot(slot("d", "s", day, "08:00", "16:00", kind="OFF"), s, tz) is None
    with pytest.raises(StoreError):
        hf.map_slot({**slot("e", "s", day, "08:00", "09:00"), "end": f"{day.isoformat()}T07:00:00+07:00"}, s, tz)


def test_bundles_and_settings_are_checked(monkeypatch):
    b = make_bundle([location("a", "OR-1", "x", "OR"), practitioner("p", "D-1", "นพ.ก")])
    assert [r["id"] for r in hf.resources_of(b)] == ["a", "p"]
    assert len(hf.resources_of([b, location("c", "OR-3", "y", "OR")])) == 3
    monkeypatch.setattr(hf, "MAX_RESOURCES", 1)
    with pytest.raises(StoreError, match="แบ่งส่ง"):
        hf.resources_of(b)
    with pytest.raises(StoreError, match="JSON"):
        hf.parse_text("{not json")
    ok = hf.clean_settings({"base_url": "https://his.example/fhir/", "token_env": "his_token", "pull_hours": 6,
                            "room_types": {"OR": "standard"}, "systems": {"employee": "urn:emp", "other": "x"}})
    assert ok["base_url"] == "https://his.example/fhir" and ok["token_env"] == "HIS_TOKEN"
    assert ok["room_types"] == {"OR": "STANDARD"} and ok["systems"] == {"employee": "urn:emp", "license": "",
                                                                          "asset": "", "room": ""}
    for bad in ({"base_url": "http://his.example/fhir"}, {"base_url": "https://u:p@his.example"},
                {"token_env": "DATABASE_URL"}, {"room_types": {"OR": "BIG"}}, {"roles": {"X": "PORTER"}},
                {"pull_hours": 500}):
        with pytest.raises(StoreError):
            hf.clean_settings(bad)
    assert hf.clean_settings({"base_url": "http://localhost:8080/fhir"})["base_url"]  # a test server


# ------------------------------------------------------------------ in the database

@pytest.fixture()
def master(store):
    return hf.MasterData(store)


def _first(store):
    return store.db.today() + timedelta(days=1)


@needs_db
def test_a_trial_then_a_real_run_and_the_same_data_again(master, store, db_url):
    data = sample(_first(store))
    trial = master.run(data, mode="file", dry_run=True, actor=ADMIN)
    assert trial["status"] == "success" and not trial["committed"]
    assert trial["stats"]["room"] == {"new": 1, "changed": 1, "unchanged": 2, "linked": 2, "skipped": 3, "failed": 0}
    assert trial["stats"]["staff"]["new"] == 4 and trial["stats"]["staff"]["skipped"] == 1  # not the pharmacist
    assert trial["stats"]["equipment"]["retired"] == 1 and trial["stats"]["equipment"]["skipped"] == 1  # not the bed
    assert trial["stats"]["roster"]["days"] == 12
    assert _rows(db_url, "SELECT count(*) FROM staff WHERE employee_code = 'D-2001'") == [(0,)]  # nothing kept
    assert _rows(db_url, "SELECT count(*) FROM his_external_refs") == [(0,)]

    real = master.run(data, mode="push", dry_run=False, by="HIS: HOSxP")
    assert real["committed"] and real["stats"] == {**trial["stats"]}
    assert _rows(db_url, "SELECT room_code, room_type, status FROM operating_rooms WHERE managed_by = 'his' ORDER BY 1") == [
        ("OR-1", "STANDARD", "AVAILABLE"), ("OR-2", "STANDARD", "AVAILABLE"), ("OR-4", "HYBRID", "MAINTENANCE"),
        ("OR-5", "NEGATIVE_PRESSURE", "AVAILABLE")]  # OR-4 keeps the type the OR team gave it
    assert _rows(db_url, "SELECT room_code FROM operating_rooms WHERE managed_by IS NULL") == [("OR-3",)]
    assert _rows(db_url, """SELECT full_name, role, specialty FROM staff WHERE managed_by = 'his' ORDER BY employee_code""") == [
        ("นพ.ธนพล ศรีสุข", "SURGEON", "General Surgery"), ("พญ.อรุณี มีสุข", "ANESTHESIOLOGIST", "Anesthesiology"),
        ("พย.กมลวรรณ ใจงาม", "SCRUB_NURSE", None), ("พย.สุนีย์ แก้วใส", "CIRCULATING_NURSE", None)]
    assert _rows(db_url, """SELECT serial_number, current_status, current_location FROM equipment_units
                            WHERE managed_by = 'his' ORDER BY 1""") == [
        ("CARM-03", "AVAILABLE", "OR-2"), ("LAP-03", "AVAILABLE", "CSSD_ZONE"), ("LAP-OLD", "RETIRED", None)]
    leave = _rows(db_url, """SELECT shift_type, note FROM staff_schedules s JOIN staff p USING (staff_id)
                             WHERE p.employee_code = 'N-3001' AND shift_type = 'LEAVE'""")
    assert leave == [("LEAVE", "ลาพักร้อน")]
    assert _rows(db_url, "SELECT count(*) FROM staff_schedules WHERE managed_by = 'his'") == [(12,)]
    assert ("staff", "Practitioner/pr-2001") in {(e, x) for e, x, _ in _rows(
        db_url, "SELECT entity_type, external_id, local_id FROM his_external_refs")}
    assert any("รับข้อมูลหลักจาก HIS" in t for (t,) in _rows(db_url, "SELECT title FROM orchestra_activity"))

    again = master.run(data, mode="push", dry_run=False, by="HIS: HOSxP")
    assert again["status"] == "success" and not again["committed"]
    assert all(again["stats"][k]["new"] == again["stats"][k]["changed"] == again["stats"][k]["linked"] == 0
               for k in ("room", "staff", "equipment"))
    assert again["stats"]["roster"]["days"] == 0
    assert _rows(db_url, "SELECT mode, dry_run, status FROM his_sync_runs ORDER BY run_id") == [
        ("file", True, "success"), ("push", False, "success"), ("push", False, "success")]


@needs_db
def test_one_bad_resource_does_not_stop_the_rest(master, store, db_url):
    first = _first(store)
    data = make_bundle([
        location("ok", "OR-6", "ห้อง 6", "OR"), location("bad", "OR 7 big", "ห้อง 7", "OR"),
        practitioner("p1", "D-1", None), role("r1", "p1", "SURGEON"),  # no name
        practitioner("p2", "D-2", "นพ.ข"), role("r2", "p2", "SURGEON"),
        schedule("s2", "p2", first, 1), slot("x1", "s2", first, "08:00", "16:00"),
        {**slot("x2", "s2", first, "08:00", "16:00"), "start": None},
        slot("x3", "nowhere", first, "08:00", "16:00"),
    ])
    report = master.run(data, mode="file", dry_run=False, actor=ADMIN)
    assert report["status"] == "partial" and report["committed"]
    assert {p["resource"] for p in report["problems"]} == {"Location/bad", "Practitioner/p1", "Slot/x2"}
    assert _rows(db_url, "SELECT count(*) FROM operating_rooms WHERE room_code = 'OR-6'") == [(1,)]
    assert _rows(db_url, """SELECT count(*) FROM staff_schedules s JOIN staff p USING (staff_id)
                            WHERE p.employee_code = 'D-2'""") == [(1,)]
    assert any(s["resource"] == "Slot/x3" for s in report["skipped"])
    outcome = hf.outcome(report)
    assert outcome["resourceType"] == "OperationOutcome" and outcome["status"] == "partial"
    assert {i["expression"][0] for i in outcome["issue"] if i["severity"] == "error"} == \
        {"Location/bad", "Practitioner/p1", "Slot/x2"}


@needs_db
def test_what_the_his_owns_is_locked_here(master, store, db_url):
    master.run(sample(_first(store)), mode="push", dry_run=False)
    admin = Inventory(store)
    person = next(p for p in admin.staff()["staff"] if p["code"] == "D-2001")
    assert person["his"] is True
    with pytest.raises(StoreError, match="มาจาก HIS"):
        admin.save_staff({**person, "name": "นพ.คนอื่น"}, staff_id=person["id"], actor=ADMIN)
    admin.save_staff({**person, "skills": ["ROBOTIC_CERTIFIED"], "max_hours_per_week": 50}, staff_id=person["id"],
                     actor=ADMIN)  # the OR team's fields
    room = next(r for r in admin.rooms()["rooms"] if r["code"] == "OR-5")
    with pytest.raises(StoreError, match="มาจาก HIS"):
        admin.save_room({**room, "status": "CLOSED"}, code="OR-5", actor=ADMIN)
    admin.save_room({**room, "capabilities": ["LAMINAR_FLOW"], "elective_start": "08:00"}, code="OR-5", actor=ADMIN)
    unit = next(u for u in admin.equipment()["units"] if u["serial"] == "CARM-03")
    other = next(t for t in admin.equipment()["types"] if t["name"] == "LAPAROSCOPY_TRAY")
    with pytest.raises(StoreError, match="มาจาก HIS"):
        admin.save_unit({"serial": "CARM-03", "type_id": other["id"], "location": "OR-1"}, serial="CARM-03", actor=ADMIN)
    admin.save_unit({"serial": "CARM-03", "type_id": unit["type_id"], "location": "OR-1"}, serial="CARM-03", actor=ADMIN)

    shifts = admin.shifts(_first(store).isoformat(), None, person["id"])["shifts"]
    assert shifts and all(s["his"] for s in shifts)
    with pytest.raises(StoreError, match="HIS"):
        admin.delete_shifts({"ids": [shifts[0]["id"]]}, actor=ADMIN)

    # an Excel file cannot change them either
    from app import hospital_io, roster_io

    wb = hospital_io.build_workbook(admin)
    from openpyxl import load_workbook

    book = load_workbook(io.BytesIO(wb))
    sheet = book[hospital_io.STAFF.title]
    for row in sheet.iter_rows(min_row=2):
        if row[0].value == "D-2001":
            row[1].value = "นพ.ชื่อใหม่"
    out = io.BytesIO()
    book.save(out)
    report = hospital_io.import_file(admin, "staff.xlsx", out.getvalue(), commit=True, actor=ADMIN)
    assert not report["committed"] and any("มาจาก HIS" in e["message"] for s in report["sheets"] for e in s["errors"])
    day = _first(store)
    csv = f"รหัสพนักงาน,{day.isoformat()}\nD-2001,07:00-15:00\n".encode()
    roster = roster_io.import_roster(admin, "roster.csv", csv, commit=True, actor=ADMIN)
    assert not roster["committed"] and any("HIS" in e["message"] for e in roster["errors"])


@needs_db
def test_rosters_replace_only_the_his_shifts(master, store, db_url):
    first = _first(store)
    admin = Inventory(store)
    master.run(sample(first), mode="push", dry_run=False)
    nurse = next(p for p in admin.staff()["staff"] if p["code"] == "N-3001")
    # typed in the app: an extra on-call the HIS knows nothing about, on a day the HIS sends
    admin.add_shifts({"type": "ON_CALL", "date_from": first.isoformat(), "date_to": first.isoformat(),
                      "start": "16:00", "end": "20:00", "staff_ids": [nurse["id"]]}, actor=ADMIN)
    data = sample(first)
    data["entry"] = [e for e in data["entry"] if e["resource"]["id"] != "s3001-1"]  # day 2's shift taken away
    report = master.run(data, mode="push", dry_run=False)
    assert report["stats"]["roster"]["days"] == 1 and report["stats"]["roster"]["removed"] == 1
    rows = _rows(db_url, """SELECT (shift_start AT TIME ZONE 'Asia/Bangkok')::date, shift_type, s.managed_by
                            FROM staff_schedules s JOIN staff p USING (staff_id) WHERE p.employee_code = 'N-3001'
                            ORDER BY 1, 2""")
    assert (first, "ON_CALL", None) in rows  # the app's own shift stays
    assert not [r for r in rows if r[0] == first + timedelta(days=1)]

    # a shift typed in the app before the HIS sends the same one: taken over, not doubled
    d7 = first + timedelta(days=8)
    admin.add_shifts({"type": "REGULAR", "date_from": d7.isoformat(), "date_to": d7.isoformat(),
                      "start": "07:30", "end": "15:30", "staff_ids": [nurse["id"]]}, actor=ADMIN)
    more = make_bundle([practitioner("pr-3001", "N-3001", "พย.กมลวรรณ ใจงาม"), role("role-3001", "pr-3001", "SCRUB_NURSE"),
                        schedule("sch-3001b", "pr-3001", d7, 1), slot("z", "sch-3001b", d7, "07:30", "15:30")])
    report = master.run(more, mode="push", dry_run=False)
    assert report["stats"]["roster"]["adopted"] == 1 and report["stats"]["roster"]["added"] == 0
    assert _rows(db_url, """SELECT count(*), count(s.managed_by) FROM staff_schedules s JOIN staff p USING (staff_id)
                            WHERE p.employee_code = 'N-3001'
                              AND (shift_start AT TIME ZONE 'Asia/Bangkok')::date = %s""", d7) == [(1, 1)]


@needs_db
def test_retired_units_are_in_no_plan(master, store):
    master.run(sample(_first(store)), mode="push", dry_run=False)
    with store._transaction(write=False) as conn:
        world = store.db.load(conn, store.db.today())
    assert "LAP-OLD" not in world.unit_ids and "CARM-03" in world.unit_ids
    with pytest.raises(StoreError, match="ไม่พบเครื่องมือ"):
        store.update_equipment("LAP-OLD", "AVAILABLE", None)
    back = make_bundle([device("dev-lap-old", "LAP-OLD", "LAPAROSCOPY_TRAY")])
    assert master.run(back, mode="push", dry_run=False)["stats"]["equipment"]["retired"] == 1  # active again


# ------------------------------------------------------------------ pulling from the HIS's FHIR server

class FakeFhir(BaseHTTPRequestHandler):
    resources: dict = {}
    token = "secret-token"
    calls: list = []
    elsewhere = False

    def log_message(self, *args):
        pass

    def _send(self, body: dict, status: int = 200):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/fhir+json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        type(self).calls.append(self.path)
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            return self._send({"resourceType": "OperationOutcome"}, 401)
        parts = urlsplit(self.path)
        kind = parts.path.rsplit("/", 1)[-1]
        if kind == "metadata":
            return self._send({"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1",
                               "software": {"name": "FakeHIS", "version": "1"},
                               "rest": [{"resource": [{"type": t} for t in hf.RESOURCE_TYPES]}]})
        items = self.resources.get(kind, [])
        page = int(parse_qs(parts.query).get("page", ["0"])[0])
        chunk = items[page * 2:(page + 1) * 2]
        links = []
        if (page + 1) * 2 < len(items):
            host = "evil.example:9" if self.elsewhere else self.headers["Host"]
            links.append({"relation": "next", "url": f"http://{host}/fhir/{kind}?page={page + 1}"})
        self._send({"resourceType": "Bundle", "type": "searchset", "link": links,
                    "entry": [{"resource": {**r, "meta": {"lastUpdated": "2026-11-01T00:00:00Z"}}} for r in chunk]})


@pytest.fixture()
def fhir_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeFhir)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    FakeFhir.calls, FakeFhir.elsewhere = [], False
    yield f"http://localhost:{server.server_address[1]}/fhir"
    server.shutdown()


@needs_db
def test_pulling_from_the_his(master, store, db_url, fhir_server, monkeypatch):
    data = sample(_first(store))
    FakeFhir.resources = {}
    for e in data["entry"]:
        FakeFhir.resources.setdefault(e["resource"]["resourceType"], []).append(e["resource"])
    with pytest.raises(StoreError, match="FHIR server"):
        master.test()  # not set up yet
    master.save_settings({**hf.DEFAULTS, "base_url": fhir_server, "token_env": "HIS_TEST_TOKEN"}, ADMIN)
    with pytest.raises(StoreError, match="HIS_TEST_TOKEN"):
        master.test()
    monkeypatch.setenv("HIS_TEST_TOKEN", "wrong")
    with pytest.raises(StoreError, match="401"):
        master.test()
    monkeypatch.setenv("HIS_TEST_TOKEN", FakeFhir.token)
    assert master.test()["fhir_version"] == "4.0.1"

    trial = master.pull(dry_run=True, actor=ADMIN)
    assert trial["mode"] == "pull" and trial["stats"]["staff"]["new"] == 4 and not trial["committed"]
    assert any("Location?_count=200" in c for c in FakeFhir.calls) and any("page=1" in c for c in FakeFhir.calls)
    assert any(c.startswith("/fhir/Slot?") and "start=ge" in c for c in FakeFhir.calls)
    real = master.pull(dry_run=False, actor=ADMIN)
    assert real["committed"]
    assert master.settings()["cursors"]["Location"] == "2026-11-01T00:00:00Z"
    FakeFhir.calls = []
    master.pull(dry_run=True, actor=ADMIN)
    assert any("_lastUpdated=gt2026-11-01" in c for c in FakeFhir.calls if c.startswith("/fhir/Location"))
    FakeFhir.elsewhere = True
    with pytest.raises(StoreError, match="เซิร์ฟเวอร์อื่น"):
        master.pull(dry_run=True, actor=ADMIN)


@needs_db
def test_the_pull_timer_runs_once_per_period(master, store, fhir_server, monkeypatch):
    timer = hf.PullTimer(master)
    assert timer.tick(now=1_000_000) is None  # no interval set
    FakeFhir.resources = {}
    monkeypatch.setenv("HIS_TEST_TOKEN", FakeFhir.token)
    master.save_settings({**hf.DEFAULTS, "base_url": fhir_server, "token_env": "HIS_TEST_TOKEN", "pull_hours": 6}, ADMIN)
    assert timer.tick(now=1_000_000)["mode"] == "pull"
    assert timer.tick(now=1_000_000 + 60) is None  # same 6 hours: claimed already
    assert timer.tick(now=1_000_000 + 6 * 3600) is not None


# ------------------------------------------------------------------ the API

@pytest.fixture()
def api(store, master, monkeypatch):
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "master", master)
    his = hm.His(store, lambda event: None)
    monkeypatch.setattr(main, "his", his)
    monkeypatch.setattr(accounts, "store", DbUserStore(store.db))
    return his


@needs_db
def test_the_his_pushes_with_a_key(api, store, db_url):
    key = api.create_key("HOSxP", ["masterdata:write"], ADMIN)["key"]
    reader = api.create_key("Dashboard", ["schedule:read"], ADMIN)["key"]
    c = TestClient(main.app, base_url="https://testserver")
    body = json.dumps(sample(_first(store)))
    fhir = {"Content-Type": "application/fhir+json"}
    assert c.post("/api/his/v1/fhir", content=body, headers=fhir).status_code == 401
    assert c.post("/api/his/v1/fhir", content=body, headers={**fhir, "Authorization": f"Bearer {reader}"}).status_code == 403
    auth = {**fhir, "Authorization": f"Bearer {key}"}
    r = c.post("/api/his/v1/fhir?dry_run=true", content=body, headers=auth)
    assert r.status_code == 200 and r.json()["resourceType"] == "OperationOutcome" and r.json()["dry_run"] is True
    assert "DRY RUN" in r.json()["issue"][0]["diagnostics"]
    r = c.post("/api/his/v1/fhir", content=body, headers=auth)
    assert r.status_code == 200 and r.json()["committed"] and r.json()["stats"]["staff"]["new"] == 4
    assert c.post("/api/his/v1/fhir", content=b"{oops", headers=auth).status_code == 400
    assert c.post("/api/his/v1/fhir", content=b"[" * 100000, headers=auth).status_code == 400  # nested too deep
    bad = json.dumps(make_bundle([location("bad", "OR 7 big", "x", "OR")]))
    r = c.post("/api/his/v1/fhir", content=bad, headers=auth)
    assert r.status_code == 422 and r.json()["status"] == "failed"
    assert _rows(db_url, "SELECT started_by FROM his_sync_runs WHERE mode = 'push' LIMIT 1") == [("HIS: HOSxP",)]


@needs_db
def test_the_admin_page(api, store, owner, db_url):
    out = owner.get("/api/admin/his/fhir").json()
    assert out["ready"] and out["push_url"].endswith("/api/his/v1/fhir") and out["scope"] == "masterdata:write"
    content = json.dumps(sample(_first(store)), ensure_ascii=False)
    r = owner.post("/api/admin/his/fhir/import", json={"content": content, "filename": "his.json", "dry_run": True})
    assert r.status_code == 200 and r.json()["report"]["dry_run"] and not r.json()["report"]["committed"]
    r = owner.post("/api/admin/his/fhir/import", json={"content": content, "filename": "his.json", "dry_run": False})
    assert r.json()["report"]["committed"] and r.json()["overview"]["managed"]["staff"] == 4
    run_id = r.json()["overview"]["runs"][0]["id"]
    assert owner.get(f"/api/admin/his/fhir/runs/{run_id}").json()["status"] == "success"
    assert owner.post("/api/admin/his/fhir/import", json={"content": "{x", "dry_run": True}).status_code == 400
    r = owner.put("/api/admin/his/fhir/settings", json={"base_url": "http://his.example", "room_types": {"OR": "STANDARD"}})
    assert r.status_code == 400 and "https" in r.json()["detail"]
    r = owner.put("/api/admin/his/fhir/settings", json={"base_url": "https://his.example/fhir", "token_env": "HIS_TOKEN",
                                                        "room_types": {"ห้องผ่าตัด": "STANDARD"}, "pull_hours": 4})
    assert r.status_code == 200 and r.json()["settings"]["room_types"] == {"ห้องผ่าตัด": "STANDARD"}
    assert r.json()["token_set"] is False
    assert owner.post("/api/admin/his/fhir/pull", json={"dry_run": True}).status_code == 400  # no token in the env
    scheduler = make_user(owner, "sched9", "scheduler")
    assert scheduler.get("/api/admin/his/fhir").status_code == 403
    assert "managed_by" not in json.dumps(owner.get("/api/admin/staff").json())  # the flag is called his
    staff = owner.get("/api/admin/staff").json()["staff"]
    assert next(p for p in staff if p["code"] == "D-2001")["his"] is True
