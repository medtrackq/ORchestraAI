"""Intake & Triage Agent: surgery requests from free text, urgency triage and
duration prediction from each surgeon's real history.

Everything here is deterministic and runs without network access:

- ``DurationPredictor`` learns from recorded actual durations
  (``scheduled_surgeries.actual_start_time / actual_end_time``). The prior is
  the surgeon's preference card (or the procedure's standard time); the
  surgeon's own history for that procedure pulls the estimate towards what
  really happens, and with no history for the procedure the surgeon's speed
  on other procedures is used. A complexity factor and a buffer for the
  spread of past cases give the recommended booking length.
- ``triage`` and ``parse_rules`` read Thai or English request text with
  keyword rules. The web app can add an LLM pass on top (``app/intake_llm``);
  these rules are the fallback and the safety net.
- ``mask_identifiers`` removes HN-like identifiers before any text leaves
  the hospital system.
"""
from __future__ import annotations

import math
import random
import re
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

# Duration multipliers by case complexity. Starting values; hospitals should
# tune them once enough cases record a complexity level.
COMPLEXITY_FACTORS = {"simple": 0.85, "standard": 1.0, "complex": 1.3}
COMPLEXITY_LABELS = {"simple": "ง่าย", "standard": "ปกติ", "complex": "ซับซ้อน"}
PRIOR_WEIGHT = 3  # the prior counts like this many past cases
BUFFER_Z = 0.52  # booking covers about 70% of comparable past cases


# ------------------------------------------------------------- prediction

@dataclass
class HistoryRecord:
    surgeon_id: str
    procedure_id: str
    minutes: float


@dataclass
class Prediction:
    procedure_id: str
    surgeon_id: str
    complexity: str
    expected_mins: int
    recommended_mins: int
    low_mins: int
    high_mins: int
    history_same: int  # this surgeon, this procedure
    history_other: int  # this surgeon, other procedures (speed factor)
    basis: str  # card | procedure
    explanation: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _round5(x: float) -> int:
    return int(5 * round(x / 5))


def _ceil5(x: float) -> int:
    return int(5 * math.ceil(x / 5))


class DurationPredictor:
    """Shrinkage estimate per surgeon and procedure (see module docstring)."""

    def __init__(self, procedures: dict[str, dict], cards: dict[tuple[str, str], dict],
                 history: list[HistoryRecord], names: dict[str, str] | None = None) -> None:
        self.procedures = procedures  # id -> {"name", "duration"}
        self.cards = cards  # (surgeon_id, procedure_id) -> {"duration", "stddev"}
        self.names = names or {}
        self.by_pair: dict[tuple[str, str], list[float]] = {}
        for rec in history:
            prior = self._prior(rec.surgeon_id, rec.procedure_id)[0]
            if prior and 0.3 * prior <= rec.minutes <= 3 * prior:  # drop obvious data-entry errors
                self.by_pair.setdefault((rec.surgeon_id, rec.procedure_id), []).append(rec.minutes)

    def _prior(self, surgeon_id: str, procedure_id: str) -> tuple[float, float, str]:
        card = self.cards.get((surgeon_id, procedure_id))
        if card and card.get("duration"):
            mean = float(card["duration"])
            return mean, float(card.get("stddev") or max(10.0, 0.2 * mean)), "card"
        proc = self.procedures.get(procedure_id) or {}
        mean = float(proc.get("duration") or 0)
        return mean, max(10.0, 0.2 * mean), "procedure"

    @property
    def history_count(self) -> int:
        return sum(len(v) for v in self.by_pair.values())

    def predict(self, surgeon_id: str, procedure_id: str, complexity: str = "standard") -> Prediction | None:
        mean0, sd0, basis = self._prior(surgeon_id, procedure_id)
        if mean0 <= 0:
            return None
        complexity = complexity if complexity in COMPLEXITY_FACTORS else "standard"
        k = PRIOR_WEIGHT
        surgeon = self.names.get(surgeon_id, "ศัลยแพทย์")
        proc_name = (self.procedures.get(procedure_id) or {}).get("name", "หัตถการนี้")
        notes = [
            f"ค่าตั้งต้น {mean0:.0f} นาที จาก preference card ของ {surgeon}" if basis == "card"
            else f"ค่าตั้งต้น {mean0:.0f} นาที จากเวลามาตรฐานของ {proc_name} (ยังไม่มี preference card)"
        ]
        same = self.by_pair.get((surgeon_id, procedure_id), [])
        others = [(p, xs) for (s, p), xs in self.by_pair.items() if s == surgeon_id and p != procedure_id]
        n_other = sum(len(xs) for _, xs in others)
        if same:
            n, m = len(same), statistics.fmean(same)
            mean = (k * mean0 + n * m) / (k + n)
            sd = sd0
            if n >= 3:
                s = statistics.stdev(same)
                sd = math.sqrt((k * sd0 ** 2 + (n - 1) * s ** 2) / (k + n - 1))
            notes.append(f"ประวัติจริงของ {surgeon} กับหัตถการนี้ {n} เคส เฉลี่ย {m:.0f} นาที "
                         f"(ถ่วงกับค่าตั้งต้นเท่ากับ {k} เคส)")
        elif n_other:
            ratios = [x / self._prior(surgeon_id, p)[0] for p, xs in others for x in xs]
            factor = (k + sum(ratios)) / (k + len(ratios))
            mean, sd = mean0 * factor, sd0 * factor
            pace = "ช้ากว่า" if factor > 1 else "เร็วกว่า"
            notes.append(f"ยังไม่มีประวัติหัตถการนี้ ใช้ความเร็วของ {surgeon} จาก {n_other} เคสอื่น: "
                         f"{pace}ค่ามาตรฐาน ×{factor:.2f}")
        else:
            mean, sd = mean0, sd0
            notes.append(f"ยังไม่มีเวลาผ่าตัดจริงของ {surgeon} ในระบบ ใช้ค่าตั้งต้น "
                         "(บันทึกเวลาจริงหลังผ่าตัดเพื่อให้ระบบเรียนรู้)")
        factor = COMPLEXITY_FACTORS[complexity]
        if factor != 1.0:
            notes.append(f"ความซับซ้อน: {COMPLEXITY_LABELS[complexity]} ×{factor:.2f}")
        mean, sd = mean * factor, sd * factor
        recommended = max(15, _ceil5(mean + BUFFER_Z * sd))
        low, high = max(10, _round5(mean - sd)), _round5(mean + sd)
        notes.append(f"แนะนำจอง {recommended} นาที (เผื่อให้ครอบคลุมราว 70% ของเคสที่ผ่านมา, "
                     f"ช่วงปกติ {low}-{high} นาที)")
        return Prediction(procedure_id, surgeon_id, complexity, round(mean), recommended, low, high,
                          len(same), 0 if same else n_other, basis, notes)


def synthetic_history(cards: list[dict], *, days: int = 40, seed: int = 7) -> list[dict]:
    """Invented past cases with actual durations, for demo databases only.

    Each surgeon gets a fixed personal pace; each case varies around it.
    Returns dicts with surgeon_id, procedure_id, days_ago, planned and actual minutes.
    """
    rng = random.Random(seed)
    pace = {}
    out = []
    for days_ago in range(2, days + 2):
        for card in cards:
            if rng.random() > 0.3:
                continue
            sid = card["surgeon_id"]
            pace.setdefault(sid, rng.choice([0.85, 0.92, 1.0, 1.08, 1.15]))
            planned = int(card["duration"])
            actual = max(15, round(planned * pace[sid] * rng.lognormvariate(0, 0.12)))
            out.append({"surgeon_id": sid, "procedure_id": card["procedure_id"], "days_ago": days_ago,
                        "planned": planned, "actual": actual})
    return out


# ------------------------------------------------------------ triage / NLP

HN_PATTERN = re.compile(r"\bHN[\s.:#-]*([A-Za-z0-9][A-Za-z0-9/-]{2,})", re.IGNORECASE)
LONG_NUMBER = re.compile(r"(?<![\d:.])\d{6,13}(?![\d:.])")

EMERGENCY_WORDS = ["ฉุกเฉิน", "emergency", "stat", "ทันที", "ด่วนที่สุด", "ด่วนมาก", "ช็อก", "shock",
                   "เลือดออก", "bleeding", "hemorrhage", "haemorrhage", "rupture", "แตก", "ทะลุ",
                   "perforat", "ไม่รู้สึกตัว", "unconscious"]
URGENT_WORDS = ["ด่วน", "urgent", "ภายในวันนี้", "ภายใน 24", "within 24", "asap", "เร็วที่สุด"]
NOT_URGENT = ["ไม่ด่วน", "not urgent", "non-urgent", "ไม่ฉุกเฉิน", "non urgent"]
COMPLEX_WORDS = ["ซับซ้อน", "เคสยาก", "complex", "redo", "re-do", "ผ่าซ้ำ", "พังผืด", "adhesion", "อ้วนมาก"]
SIMPLE_WORDS = ["ไม่ซับซ้อน", "ง่าย", "simple", "uncomplicated"]
DIRTY_WORDS = ["หนอง", "ฝี", "ทะลุ", "perforat", "dirty", "infected", "ติดเชื้อ", "abscess"]
# Airborne infection: the case needs a negative-pressure room (OR Facility Agent).
AIRBORNE_PHRASES = ["ติดเชื้อทางอากาศ", "แพร่เชื้อทางอากาศ", "ห้องความดันลบ", "ห้องแรงดันลบ"]
AIRBORNE_WORDS = AIRBORNE_PHRASES + [
    "วัณโรค", "โรคหัด", "อีสุกอีใส", "โควิด", "tuberculosis", "measles", "varicella", "chickenpox",
    "covid", "airborne", "negative pressure", "mers-cov", "sars-cov",
]
_AIRBORNE_ABBR = re.compile(r"(?<![a-z0-9])(tb|mdr-tb|xdr-tb)(?![a-z0-9])")


def is_airborne(text: str) -> bool:
    low = (text or "").lower()
    return any(_has(low, w) for w in AIRBORNE_WORDS) or _AIRBORNE_ABBR.search(low) is not None

THAI_HINTS = {
    "ถุงน้ำดี": ["cholecyst"], "นิ่วในถุงน้ำดี": ["cholecyst"], "ไส้ติ่ง": ["append"],
    "ไส้เลื่อน": ["hernia"], "ข้อเข่า": ["knee"], "เข่า": ["knee"], "เอ็นไขว้": ["acl", "cruciate"],
    "ต่อมลูกหมาก": ["prostat"], "ไต": ["nephrect"], "ฝี": ["abscess", "drainage"], "หนอง": ["abscess", "drainage"],
    "บายพาส": ["bypass"], "หลอดเลือด": ["bypass", "vascular"], "เปิดหน้าท้อง": ["laparotomy"],
    "สำรวจช่องท้อง": ["laparotomy"], "ลำไส้": ["laparotomy", "bowel"], "ส่องกล้อง": ["laparoscop", "arthroscop"],
    "หุ่นยนต์": ["robotic"], "เปิดกะโหลก": ["craniotomy"], "สมอง": ["craniotomy"], "นิ่วในท่อไต": ["ureteroscop"],
    "เปลี่ยนข้อ": ["arthroplasty", "total"], "ผ่าเปิด": ["open"], "แบบเปิด": ["open"], "ฉุกเฉิน": ["emergency"],
    "ด่วนมาก": ["emergency"], "สำรวจ": ["exploratory"],
}
ABBREVIATIONS = {
    "lap chole": ["cholecyst"], "lc ": ["cholecyst"], "tka": ["knee", "arthroplasty"],
    "tkr": ["knee", "arthroplasty"], "acl": ["acl"], "turp": ["transurethral", "prostat"],
    "tep": ["hernia"], "appy": ["append"], "ex lap": ["laparotomy"], "i&d": ["abscess", "drainage"],
    "urs": ["ureteroscop"], "fem-pop": ["femoral", "bypass"],
}
_STOP = {"with", "and", "the", "for"}  # qualifiers (open, total, emergency...) still count, so the
# plain request "ไส้ติ่ง" prefers "Laparoscopic appendectomy" over "Emergency appendectomy (open)"

THAI_NUMBERS = {"หนึ่ง": 1, "สอง": 2, "สาม": 3, "สี่": 4, "ห้า": 5, "หก": 6, "เจ็ด": 7, "แปด": 8, "เก้า": 9,
                "สิบ": 10, "สิบเอ็ด": 11}
THAI_WEEKDAYS = {"จันทร์": 0, "อังคาร": 1, "พุธ": 2, "พฤหัส": 3, "ศุกร์": 4, "เสาร์": 5, "อาทิตย์": 6}
EN_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}


def mask_identifiers(text: str) -> tuple[str, str | None]:
    """Replace HN and long ID numbers with placeholders. Returns (masked, first HN)."""
    match = HN_PATTERN.search(text or "")
    hn = match.group(1) if match else None
    masked = HN_PATTERN.sub("HN [HN]", text or "")
    masked = LONG_NUMBER.sub("[ID]", masked)
    return masked, hn


def _has(low: str, word: str) -> bool:
    """English words match at a word start ("stat" must not hit "prostatectomy");
    Thai has no spaces between words, so Thai matches anywhere."""
    if word.isascii():
        return re.search(r"(?<![a-z])" + re.escape(word), low) is not None
    return word in low


def triage(text: str) -> tuple[str, str]:
    low = (text or "").lower()
    negated = [w for w in NOT_URGENT if w in low]
    for neg in negated:
        low = low.replace(neg, " ")
    for word in EMERGENCY_WORDS:
        if _has(low, word):
            return "EMERGENCY", f"พบคำว่า \"{word}\" ซึ่งบ่งชี้เคสฉุกเฉิน"
    for word in URGENT_WORDS:
        if _has(low, word):
            return "URGENT", f"พบคำว่า \"{word}\" ซึ่งบ่งชี้เคสด่วน"
    if negated:
        return "ELECTIVE", f"ระบุว่า \"{negated[0]}\" จัดเป็นเคสนัดล่วงหน้า (Elective)"
    return "ELECTIVE", "ไม่พบคำบ่งชี้ความเร่งด่วน จัดเป็นเคสนัดล่วงหน้า (Elective)"


def _name_core(name: str) -> list[str]:
    name = re.sub(r"\(.*?\)", " ", name)
    name = re.sub(r"^(dr\.?|นพ\.|พญ\.|หมอ)\s*", "", name.strip(), flags=re.IGNORECASE)
    return [t for t in re.split(r"[\s.]+", name.lower()) if len(t) >= 3]


def _match_procedure(text: str, procedures: list[dict]) -> tuple[dict | None, float]:
    low = " " + (text or "").lower() + " "
    hints = [h for word, hs in THAI_HINTS.items() if word in low for h in hs]
    hints += [h for abbr, hs in ABBREVIATIONS.items() if abbr in low for h in hs]
    best, best_score = None, 0.0
    for proc in procedures:
        tokens = [t for t in re.split(r"[^a-z0-9]+", proc["name"].lower()) if len(t) >= 3 and t not in _STOP]
        if not tokens:
            continue
        hits = sum(1 for t in tokens if t[:6] in low or any(h in t or t.startswith(h) for h in hints))
        score = hits / len(tokens)
        if proc["name"].lower() in low:
            score = 2.0
        if score > best_score:
            best, best_score = proc, score
    return (best, best_score) if best_score >= 0.3 else (None, best_score)


def _parse_time(text: str) -> str | None:
    low = (text or "").lower()
    m = re.search(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)", low)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    num = r"(\d{1,2}|" + "|".join(sorted(THAI_NUMBERS, key=len, reverse=True)) + r")"
    to_int = lambda s: int(s) if s.isdigit() else THAI_NUMBERS[s]  # noqa: E731
    m = re.search(r"บ่าย\s*" + num + r"?\s*(โมง)?", low)
    if m and (m.group(1) or m.group(2)):
        return f"{12 + (to_int(m.group(1)) if m.group(1) else 1):02d}:00"
    m = re.search(num + r"\s*โมง\s*(เย็น|เช้า)?", low)
    if m:
        hour = to_int(m.group(1)) + (12 if m.group(2) == "เย็น" else 0)
        if 0 <= hour <= 23:
            return f"{hour:02d}:00"
    m = re.search(r"(?<!\d)(\d{1,2})\s*(am|pm|a\.m\.|p\.m\.)", low)
    if m:
        hour = int(m.group(1)) % 12 + (12 if m.group(2).startswith("p") else 0)
        return f"{hour:02d}:00"
    if "เที่ยง" in low or "noon" in low:
        return "12:00"
    if "บ่าย" in low or "afternoon" in low:
        return "13:00"
    if "เช้า" in low or "morning" in low:
        return "08:00"
    return None


def _parse_date(text: str, today: date) -> date | None:
    low = (text or "").lower()
    if "มะรืน" in low or "day after tomorrow" in low:
        return today + timedelta(days=2)
    if "พรุ่งนี้" in low or "tomorrow" in low:
        return today + timedelta(days=1)
    if "วันนี้" in low or "today" in low:
        return today
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", low)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"(?<![\d:.])(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?(?![\d:])", low)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2500 if year > 50 else 2000
        if year > 2400:  # Buddhist era
            year -= 543
        try:
            return date(year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
    for names in (THAI_WEEKDAYS, EN_WEEKDAYS):
        for name, wd in names.items():
            if name in low:
                return today + timedelta(days=(wd - today.weekday()) % 7 or 7)
    return None


def _parse_duration(text: str) -> int | None:
    low = (text or "").lower()
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(นาที|min|minutes|ชั่วโมง|ชม\.?|hours?|hrs?)", low):
        before = low[max(0, m.start() - 8):m.start()]
        if "ภายใน" in before or "within" in before:
            continue  # "within 24 h" is a deadline, not a duration
        value = float(m.group(1))
        minutes = value if m.group(2) in ("นาที", "min", "minutes") else value * 60
        if 10 <= minutes <= 900:
            return int(round(minutes))
    return None


def parse_rules(text: str, procedures: list[dict], surgeons: list[dict], today: date) -> dict:
    """Keyword-based reading of a request. Returns form fields plus what was found."""
    low = (text or "").lower()
    proc, score = _match_procedure(text, procedures)
    surgeon = None
    for s in surgeons:
        if any(tok in low for tok in _name_core(s["name"])):
            surgeon = s
            break
    urgency, reason = triage(text)
    complexity = "standard"
    if any(_has(low, w) for w in SIMPLE_WORDS):  # first: "ไม่ซับซ้อน" contains "ซับซ้อน"
        complexity = "simple"
    elif any(_has(low, w) for w in COMPLEX_WORDS):
        complexity = "complex"
    infection = "CLEAN"
    airborne = is_airborne(text)
    wound = low
    for phrase in AIRBORNE_PHRASES:  # "ติดเชื้อทางอากาศ" is about the patient, not a dirty wound
        wound = wound.replace(phrase, " ")
    if any(_has(wound, w) for w in DIRTY_WORDS):
        infection = "DIRTY"
    elif _has(low, "contaminated"):
        infection = "CONTAMINATED"
    when = _parse_date(text, today)
    _, hn = mask_identifiers(text)
    return {
        "procedure_id": proc["id"] if proc else None,
        "procedure": proc["name"] if proc else None,
        "procedure_score": round(score, 2),
        "surgeon_id": surgeon["id"] if surgeon else None,
        "urgency": urgency,
        "urgency_reason": reason,
        "date": when.isoformat() if when else None,
        "start": _parse_time(text),
        "duration_mins": _parse_duration(text),
        "complexity": complexity,
        "requires_icu_post_op": _has(low, "icu") or "ไอซียู" in low,
        "infection_risk": infection,
        "airborne": airborne,
        "patient_hn": hn,
        "notes": None,
    }
