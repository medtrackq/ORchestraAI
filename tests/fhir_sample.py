"""A hospital's master data as an HIS would send it (HL7 FHIR R4), for the tests and the sample file
(docs/integration/sample-bundle.json: ``python tests/fhir_sample.py 2026-12-01``)."""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta

SYS = "https://hospital.example/fhir"
ROLE_SYS = f"{SYS}/CodeSystem/or-role"
TZ = "+07:00"


def location(id_, code, name, type_code=None, *, status="active", physical="ro", part_of=None):
    r = {"resourceType": "Location", "id": id_, "status": status, "name": name,
         "physicalType": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/location-physical-type",
                                      "code": physical}]}}
    if code:
        r["identifier"] = [{"system": f"{SYS}/location", "value": code}]
    if type_code:
        r["type"] = [{"coding": [{"system": f"{SYS}/CodeSystem/location-type", "code": type_code}]}]
    if part_of:
        r["partOf"] = {"reference": f"Location/{part_of}"}
    return r


def practitioner(id_, employee, name, *, license_no=None, active=True, prefix=None, given=None, family=None):
    ids = [{"system": f"{SYS}/employee", "type": {"coding": [{"code": "EI"}]}, "value": employee}]
    if license_no:
        ids.append({"system": f"{SYS}/license", "type": {"coding": [{"code": "PRN"}]}, "value": license_no})
    human = {"use": "official"}
    if name:
        human["text"] = name
    if prefix:
        human.update(prefix=[prefix], given=[given], family=family)
    return {"resourceType": "Practitioner", "id": id_, "active": active, "identifier": ids, "name": [human]}


def role(id_, who, code, specialty=None, *, active=True):
    r = {"resourceType": "PractitionerRole", "id": id_, "active": active, "practitioner": {"reference": f"Practitioner/{who}"},
         "code": [{"coding": [{"system": ROLE_SYS, "code": code}]}]}
    if specialty:
        r["specialty"] = [{"text": specialty}]
    return r


def device(id_, asset, type_code, *, where=None, status="active", display=None):
    r = {"resourceType": "Device", "id": id_, "status": status,
         "identifier": [{"system": f"{SYS}/asset", "value": asset}],
         "type": {"coding": [{"system": f"{SYS}/CodeSystem/device-type", "code": type_code,
                              **({"display": display} if display else {})}]}}
    if where:
        r["location"] = {"reference": f"Location/{where}"}
    return r


def schedule(id_, who, first: date, days: int):
    return {"resourceType": "Schedule", "id": id_, "active": True, "actor": [{"reference": f"Practitioner/{who}"}],
            "planningHorizon": {"start": f"{first.isoformat()}T00:00:00{TZ}",
                                "end": f"{(first + timedelta(days=days)).isoformat()}T00:00:00{TZ}"}}


def slot(id_, sched, day: date, start: str, end: str, *, status="free", kind=None, comment=None):
    end_day = day + timedelta(days=1) if end <= start else day
    r = {"resourceType": "Slot", "id": id_, "schedule": {"reference": f"Schedule/{sched}"}, "status": status,
         "start": f"{day.isoformat()}T{start}:00{TZ}", "end": f"{end_day.isoformat()}T{end}:00{TZ}"}
    if kind:
        r["serviceType"] = [{"coding": [{"system": f"{SYS}/CodeSystem/shift-type", "code": kind}]}]
    if comment:
        r["comment"] = comment
    return r


def bundle(resources: list[dict]) -> dict:
    return {"resourceType": "Bundle", "type": "collection",
            "entry": [{"fullUrl": f"{SYS}/{r['resourceType']}/{r['id']}", "resource": r} for r in resources]}


def sample(first: date) -> dict:
    """Rooms (2 there already in the demo, 1 new, 1 under maintenance, a ward, CSSD), the OR team and a
    pharmacist, OR equipment and a hospital bed, and one week of rosters for two people."""
    week = [first + timedelta(days=i) for i in range(7)]
    resources = [
        location("or-suite", None, "อาคารผ่าตัด ชั้น 3", physical="lvl"),
        location("loc-cssd", "CSSD", "หน่วยจ่ายกลาง (CSSD)", "CSSD", part_of="or-suite"),
        location("loc-or1", "OR-1", "ห้องผ่าตัด 1", "OR", part_of="or-suite"),
        location("loc-or2", "OR-2", "ห้องผ่าตัด 2", "OR", part_of="or-suite"),
        location("loc-or4", "OR-4", "ห้องผ่าตัด 4", "OR", status="suspended", part_of="or-suite"),
        location("loc-or5", "OR-5", "ห้องผ่าตัดความดันลบ", "OR-NEG", part_of="or-suite"),
        location("loc-ward", "W-SURG", "หอผู้ป่วยศัลยกรรมชาย", "WARD"),
        practitioner("pr-2001", "D-2001", "นพ.ธนพล ศรีสุข", license_no="ว.45678"),
        role("role-2001", "pr-2001", "SURGEON", "General Surgery"),
        practitioner("pr-2002", "D-2002", None, license_no="ว.56789", prefix="พญ.", given="อรุณี", family="มีสุข"),
        role("role-2002", "pr-2002", "ANESTHESIOLOGIST", "Anesthesiology"),
        practitioner("pr-3001", "N-3001", "พย.กมลวรรณ ใจงาม"),
        role("role-3001", "pr-3001", "SCRUB_NURSE"),
        practitioner("pr-3002", "N-3002", "พย.สุนีย์ แก้วใส"),
        role("role-3002", "pr-3002", "CIRCULATING_NURSE"),
        practitioner("pr-4001", "P-4001", "ภก.วิชัย ยาดี"),
        role("role-4001", "pr-4001", "PHARMACIST"),
        device("dev-lap-03", "LAP-03", "LAPAROSCOPY_TRAY", where="loc-cssd"),
        device("dev-carm-03", "CARM-03", "C_ARM", where="loc-or2"),
        device("dev-lap-old", "LAP-OLD", "LAPAROSCOPY_TRAY", status="inactive"),
        device("dev-bed-77", "BED-77", "HOSPITAL_BED", display="เตียงผู้ป่วย"),
        schedule("sch-2001", "pr-2001", first, 7),
        schedule("sch-3001", "pr-3001", first, 7),
    ]
    for i, day in enumerate(week[:5]):
        resources.append(slot(f"s2001-{i}", "sch-2001", day, "08:00", "16:00"))
        resources.append(slot(f"s3001-{i}", "sch-3001", day, "07:30", "15:30"))
    resources.append(slot("s2001-oc", "sch-2001", week[5], "16:00", "08:00", status="busy", kind="ON_CALL"))
    resources.append(slot("s3001-leave", "sch-3001", week[6], "08:00", "16:00", status="busy-unavailable",
                          comment="ลาพักร้อน"))
    return bundle(resources)


if __name__ == "__main__":
    start = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date.today() + timedelta(days=1)
    print(json.dumps(sample(start), ensure_ascii=False, indent=2))
