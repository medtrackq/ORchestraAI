"""Optional LLM pass for the Intake & Triage Agent (Claude API).

Used only when ANTHROPIC_API_KEY is set. Privacy by design:
- the text is masked first (HN and long ID numbers removed) and the prompt
  asks the request not to carry patient names;
- procedures and surgeons are sent as short codes ("P3", "S2"), never
  database ids;
- the answer is constrained to a JSON schema and mapped back to codes the
  hospital catalogue actually has. Any failure falls back to the keyword
  rules, so intake never depends on the API being reachable.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date

log = logging.getLogger("orchestra.intake")

DEFAULT_MODEL = "claude-opus-5-5"
SYSTEM = (
    "You read operating-room booking requests written by Thai hospital staff (Thai or English, often "
    "abbreviated) and fill a booking form. Pick the procedure and surgeon only from the codes listed; "
    "use an empty string when nothing fits. Urgency: EMERGENCY for life- or limb-threatening cases that "
    "need surgery now, URGENT for cases that must be done within about 24 hours, otherwise ELECTIVE. "
    "Write urgency_reason (one short sentence) and notes in Thai. Resolve relative dates against the planning "
    "date given. duration_mins is 0 unless the text states a duration. airborne is true only when the "
    "patient has or is suspected of an airborne infection (e.g. TB, measles, varicella, COVID-19) and needs "
    "a negative-pressure room. Patient identifiers are masked; never guess them."
)


class IntakeUnavailable(Exception):
    """The LLM pass could not run; the caller falls back to rules."""


def _schema(procedure_codes: list[str], surgeon_codes: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "procedure_code": {"type": "string", "enum": procedure_codes + [""]},
            "procedure_text": {"type": "string"},
            "surgeon_code": {"type": "string", "enum": surgeon_codes + [""]},
            "urgency": {"type": "string", "enum": ["ELECTIVE", "URGENT", "EMERGENCY"]},
            "urgency_reason": {"type": "string"},
            "date": {"type": "string", "description": "YYYY-MM-DD or empty"},
            "start_time": {"type": "string", "description": "HH:MM 24-hour or empty"},
            "duration_mins": {"type": "integer"},
            "complexity": {"type": "string", "enum": ["simple", "standard", "complex"]},
            "requires_icu": {"type": "boolean"},
            "infection_risk": {"type": "string", "enum": ["CLEAN", "CONTAMINATED", "DIRTY"]},
            "airborne": {"type": "boolean"},
            "notes": {"type": "string"},
        },
        "required": ["procedure_code", "procedure_text", "surgeon_code", "urgency", "urgency_reason", "date",
                     "start_time", "duration_mins", "complexity", "requires_icu", "infection_risk", "airborne",
                     "notes"],
        "additionalProperties": False,
    }


class ClaudeIntake:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or None
        self.model = model or os.environ.get("INTAKE_MODEL") or DEFAULT_MODEL
        self._client = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=30.0, max_retries=1)
        return self._client

    def parse(self, masked_text: str, procedures: list[dict], surgeons: list[dict], today: date) -> dict:
        """Return form fields from masked text, or raise IntakeUnavailable."""
        if not self.enabled:
            raise IntakeUnavailable("ยังไม่ได้ตั้ง ANTHROPIC_API_KEY")
        import anthropic

        proc_codes = {f"P{i}": p for i, p in enumerate(procedures, 1)}
        surg_codes = {f"S{i}": s for i, s in enumerate(surgeons, 1)}
        catalogue = "\n".join(
            ["Procedures:"] + [f"{c}: {p['name']}" for c, p in proc_codes.items()]
            + ["", "Surgeons:"] + [f"{c}: {s['name']}" for c, s in surg_codes.items()]
        )
        try:
            response = self._get_client().beta.messages.create(
                model=self.model,
                max_tokens=2048,
                system=SYSTEM,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": _schema(list(proc_codes), list(surg_codes))},
                },
                messages=[{
                    "role": "user",
                    "content": f"{catalogue}\n\nPlanning date: {today.isoformat()} "
                               f"({today.strftime('%A')})\n\nRequest:\n{masked_text}",
                }],
            )
        except anthropic.AuthenticationError as exc:
            raise IntakeUnavailable("ANTHROPIC_API_KEY ไม่ถูกต้อง") from exc
        except anthropic.RateLimitError as exc:
            raise IntakeUnavailable("Claude API ถูกจำกัดอัตราการใช้งานชั่วคราว") from exc
        except anthropic.APITimeoutError as exc:
            raise IntakeUnavailable("Claude API ตอบช้าเกินไป") from exc
        except anthropic.APIConnectionError as exc:
            raise IntakeUnavailable("เชื่อมต่อ Claude API ไม่ได้") from exc
        except anthropic.APIStatusError as exc:
            log.warning("intake LLM error %s: %s", exc.status_code, exc)
            raise IntakeUnavailable(f"Claude API ตอบกลับผิดพลาด ({exc.status_code})") from exc

        if response.stop_reason == "refusal":
            raise IntakeUnavailable("Claude ไม่ประมวลผลข้อความนี้")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise IntakeUnavailable("ผลลัพธ์จาก Claude อ่านไม่ได้") from exc

        proc = proc_codes.get(data.get("procedure_code") or "")
        surgeon = surg_codes.get(data.get("surgeon_code") or "")
        return {
            "procedure_id": proc["id"] if proc else None,
            "procedure": proc["name"] if proc else (data.get("procedure_text") or None),
            "surgeon_id": surgeon["id"] if surgeon else None,
            "urgency": data.get("urgency") or "ELECTIVE",
            "urgency_reason": data.get("urgency_reason") or "",
            "date": data.get("date") or None,
            "start": data.get("start_time") or None,
            "duration_mins": data.get("duration_mins") or None,
            "complexity": data.get("complexity") or "standard",
            "requires_icu_post_op": bool(data.get("requires_icu")),
            "infection_risk": data.get("infection_risk") or "CLEAN",
            "airborne": bool(data.get("airborne")),
            "notes": data.get("notes") or None,
        }
