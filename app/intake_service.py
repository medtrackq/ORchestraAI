"""Intake & Triage Agent service: free text -> booking form + duration forecast."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from orchestra_engine.intake import (
    COMPLEXITY_FACTORS,
    DurationPredictor,
    HistoryRecord,
    mask_identifiers,
    parse_rules,
)

from .intake_llm import ClaudeIntake, IntakeUnavailable
from .store import StoreError

_SEVERITY = {"ELECTIVE": 0, "URGENT": 1, "EMERGENCY": 2}
_INFECTION = {"CLEAN": 0, "CONTAMINATED": 1, "DIRTY": 2}


@dataclass
class IntakeContext:
    """What the agent may pick from on the planning day."""

    today: date
    procedures: list[dict]  # id, name, duration, caps, icu, infection, skills
    surgeons: list[dict]  # id, name
    cards: list[dict]  # surgeon_id, procedure_id, duration, stddev, roles, equipment
    history: list[HistoryRecord] = field(default_factory=list)

    def predictor(self) -> DurationPredictor:
        return DurationPredictor(
            {p["id"]: p for p in self.procedures},
            {(c["surgeon_id"], c["procedure_id"]): c for c in self.cards},
            self.history,
            {s["id"]: s["name"] for s in self.surgeons},
        )


class IntakeService:
    def __init__(self, llm: ClaudeIntake | None = None) -> None:
        self.llm = llm or ClaudeIntake()

    def info(self, ctx: IntakeContext) -> dict:
        return {"llm_enabled": self.llm.enabled, "model": self.llm.model if self.llm.enabled else None,
                "history_count": ctx.predictor().history_count}

    def predict(self, ctx: IntakeContext, surgeon_id: str, procedure_id: str, complexity: str = "standard") -> dict | None:
        if complexity not in COMPLEXITY_FACTORS:
            raise StoreError(f"ระดับความซับซ้อนไม่ถูกต้อง: {complexity}")
        prediction = ctx.predictor().predict(surgeon_id, procedure_id, complexity)
        return prediction.to_dict() if prediction else None

    def parse(self, ctx: IntakeContext, text: str, *, only_surgeon: str | None = None) -> dict:
        text = (text or "").strip()
        if len(text) < 3:
            raise StoreError("กรุณาพิมพ์หรือพูดคำขอผ่าตัด")
        masked, hn = mask_identifiers(text)
        rules = parse_rules(text, ctx.procedures, ctx.surgeons, ctx.today)
        fields, warnings, source = dict(rules), [], "rules"
        if self.llm.enabled:
            try:
                ai = self.llm.parse(masked, ctx.procedures, ctx.surgeons, ctx.today)
                fields.update({k: v for k, v in ai.items() if v not in (None, "")})
                source = "claude"
            except IntakeUnavailable as exc:
                warnings.append(f"ใช้ตัวแยกคำแบบกฎแทน Claude: {exc}")
        # Safety net: never let a softer reading downgrade urgency the keyword rules found.
        if _SEVERITY[rules["urgency"]] > _SEVERITY.get(fields["urgency"], 0):
            fields["urgency"], fields["urgency_reason"] = rules["urgency"], rules["urgency_reason"] + " (กฎความปลอดภัย)"
        fields["airborne"] = bool(rules["airborne"] or fields.get("airborne"))  # isolation is never dropped
        if fields["airborne"]:
            warnings.append("ผู้ป่วยติดเชื้อทางอากาศ: ต้องใช้ห้องความดันลบ (NEGATIVE_PRESSURE) และทำความสะอาดห้องนานขึ้น")
        fields["patient_hn"] = hn
        if only_surgeon:
            if fields.get("surgeon_id") and fields["surgeon_id"] != only_surgeon:
                warnings.append("บัญชีศัลยแพทย์จองได้เฉพาะเคสของตัวเอง จึงใส่ชื่อคุณเป็นแพทย์ผ่าตัด")
            fields["surgeon_id"] = only_surgeon

        proc = next((p for p in ctx.procedures if p["id"] == fields.get("procedure_id")), None)
        card = None
        if proc:
            fields["procedure"] = proc["name"]
            fields["required_capabilities"] = list(proc.get("caps") or [])
            fields["required_staff_skills"] = dict(proc.get("skills") or {})
            fields["requires_icu_post_op"] = bool(fields.get("requires_icu_post_op")) or bool(proc.get("icu"))
            if _INFECTION.get(proc.get("infection") or "CLEAN", 0) > _INFECTION.get(fields.get("infection_risk"), 0):
                fields["infection_risk"] = proc["infection"]
            card = next((c for c in ctx.cards if c["procedure_id"] == proc["id"]
                         and c["surgeon_id"] == fields.get("surgeon_id")), None)
        if card:
            fields["required_staff_roles"] = card.get("roles") or None
            fields["required_equipment_types"] = list(card.get("equipment") or [])

        prediction = None
        if proc and fields.get("surgeon_id"):
            prediction = self.predict(ctx, fields["surgeon_id"], proc["id"], fields.get("complexity") or "standard")
        stated = fields.get("duration_mins")
        if stated:
            fields["duration_source"] = "ระบุในคำขอ"
        elif prediction:
            fields["duration_mins"] = prediction["recommended_mins"]
            fields["duration_source"] = "พยากรณ์จากประวัติ"
        elif proc:
            fields["duration_mins"] = proc.get("duration")
            fields["duration_source"] = "เวลามาตรฐานของหัตถการ"

        missing = [label for key, label in (("surgeon_id", "ศัลยแพทย์"), ("procedure_id", "หัตถการ"),
                                            ("date", "วันที่"), ("start", "เวลา"), ("patient_hn", "HN"))
                   if not fields.get(key)]
        if missing:
            warnings.append("ไม่พบในข้อความ: " + ", ".join(missing) + " (กรอกเพิ่มในฟอร์ม)")
        if fields.get("date") and fields["date"] < ctx.today.isoformat():
            warnings.append(f"วันที่ {fields['date']} ผ่านไปแล้ว")
        return {"fields": fields, "prediction": prediction, "source": source, "warnings": warnings}
