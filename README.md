# ORchestraAI — ระบบนัดผ่าตัดอัจฉริยะ

ระบบจัดตารางห้องผ่าตัดแบบ Multi-Agent ที่ประสานทรัพยากร 3 ด้าน ได้แก่ **ทีมงาน**, **เครื่องมือ/CSSD** และ **ห้องผ่าตัด + เตียง PACU/ICU**
แล้วส่งให้ **Google OR-Tools CP-SAT** คำนวณตารางที่ไม่ชนกัน รองรับเคสฉุกเฉินแทรกและการผ่าตัดยืดเยื้อ (Ripple Effect)

## โครงสร้าง

| Path | เนื้อหา |
|---|---|
| `supabase/migrations/20261031000000_initial_schema.sql` | Schema PostgreSQL (ตรงกับที่สร้างไว้ใน Supabase) |
| `.claude/skills/orchestra-ai-scheduler/SKILL.md` | Skill อธิบายสถาปัตยกรรม Agent และเงื่อนไข |
| `orchestra_engine/models.py` | `OperatingRoom`, `StaffMember`, `EquipmentUnit`, `RecoveryBed`, `SurgeryCase` |
| `orchestra_engine/optimizer.py` | `ORchestraOptimizer` — ตรวจทรัพยากร (agents) + โมเดล CP-SAT + reschedule + `check_slot` |
| `orchestra_engine/agents.py` | Staffing / Equipment & CSSD / OR Facility Agent สแกน ณ เวลาที่ขอ (`SlotCheck`) |
| `orchestra_engine/flow.py` | สร้างข้อมูลจริงให้หน้า Flow (`python -m orchestra_engine.flow`) |
| `app/` | เว็บแอป FastAPI (หน้าเว็บ `app/static/index.html` + API) สำหรับ Render |
| `render.yaml` | Render Blueprint (web service ฟรี + รหัสผ่านอัตโนมัติ) |
| `web/orchestra-flow.html` | หน้า interactive 5 ขั้น (ปรับจาก ORchestraAIFlow.html) แสดงผลจาก engine จริง |
| `orchestra_engine/validation.py` | ตัวตรวจ Hard Constraint อิสระ (ไม่ใช้โค้ด solver) |
| `orchestra_engine/demo.py` | โรงพยาบาลจำลอง + 4 สถานการณ์ (ข้อมูลสมมติทั้งหมด) |
| `tests/` | pytest ครอบคลุมทุก Hard Constraint และ Ripple Effect |

## เข้าใช้งานผ่านเว็บ (Render)

repo มี `render.yaml` (Render Blueprint) สำหรับ deploy เว็บแอป `app/` เป็น service เดียว (หน้าเว็บ + API)

1. Render Dashboard → **New → Blueprint** → เลือก repo `medtrackq/ORchestraAI` (branch `main`) → **Apply**
2. รอ build เสร็จ (ครั้งแรกประมาณ 3-5 นาที) แล้วเปิด URL ของ service `orchestra-ai` (เช่น `https://orchestra-ai.onrender.com`)
3. ล็อกอิน: ชื่อผู้ใช้ `orchestra` รหัสผ่านดูได้ที่ service → **Environment** → `APP_PASSWORD` (Render สุ่มให้ เปลี่ยนเองได้)

หน้าเว็บ: ตารางทุกห้อง · จองผ่าตัด (Agent ตรวจเวลา + เสนอช่วงใหม่) · แทรกเคสฉุกเฉิน · ผ่าตัดเกินเวลา · ประวัติกิจกรรม/แจ้งเตือน · `/flow` หน้า Flow สาธิต · `/api/docs` เอกสาร API

ข้อจำกัดตอนนี้: ใช้ข้อมูลจำลองในหน่วยความจำ (รีเซ็ตเมื่อ service restart) และ plan ฟรีของ Render จะหลับเมื่อไม่มีคนใช้ 15 นาที เปิดครั้งแรกหลังจากนั้นรอประมาณ 1 นาที การเชื่อม Supabase จะมาแทนในรอบถัดไป

รันเว็บบนเครื่อง:

```bash
.venv/bin/pip install -e ".[dev]" && APP_PASSWORD=changeme .venv/bin/uvicorn app.main:app --reload
```

## ติดตั้งและรัน

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

```bash
.venv/bin/python -m orchestra_engine.demo
```

```bash
.venv/bin/python -m pytest -q
```

Demo มี 4 สถานการณ์: (1) วันผ่าตัดปกติ 10 เคส 4 ห้อง (2) ชุดเข่ามีชุดเดียวและยังอบอยู่ที่ CSSD (3) เคส EMERGENCY เข้ามาตอน 10:30 (4) เคสหุ่นยนต์ผ่าตัดยืดเยื้อ 60 นาที แต่ละผลถูกตรวจซ้ำด้วย `validate_schedule`

## การใช้งาน

```python
from orchestra_engine import ORchestraOptimizer, OperatingRoom, EquipmentUnit, StaffMember, SurgeryCase, hm

optimizer = ORchestraOptimizer(rooms, equipments, staff, beds, team_synergy=synergy)
day = optimizer.solve_schedule(cases, "Scenario Name")

# เคสฉุกเฉินแทรก: เคสที่เริ่มไปแล้วถูกล็อก เคสอื่นขยับให้น้อยที่สุด
after = optimizer.insert_emergency(day, cases, [emergency_case], now_min=hm("10:30"))
# ผ่าตัดยืดเยื้อ
after = optimizer.handle_overrun(day, cases, "C04", new_duration_mins=270, now_min=hm("11:00"))
after.changes        # เคสที่เปลี่ยน (เวลา/ห้อง/ทีม/เครื่องมือ/เตียง)
after.notifications  # ข้อความแจ้งเตือนรายบุคคล

# ขอเวลาเจาะจง: Agent 3 ตัวสแกนพร้อมกัน ถ้าผ่านหมดล็อกเลย ถ้าไม่ผ่าน CP-SAT หาช่วงถัดไปที่พร้อมครบ
check = optimizer.check_slot(day, new_case, hm("09:00"))   # เคสที่จองไว้แล้วไม่ถูกขยับ
check.verdicts       # ผลของแต่ละ Agent + เวลาที่ทรัพยากรของตัวเองพร้อม
check.assignment     # ช่วงที่ล็อกได้ (หรือ None พร้อมเหตุผลใน check.result.unscheduled)
check.log            # Negotiation log

day.to_dict(), check.to_dict()   # JSON สำหรับ API / หน้าเว็บ
```

## หน้า Flow (`web/orchestra-flow.html`)

เปิดไฟล์ในเบราว์เซอร์ได้เลย (ไม่ต้องมี server) กดปุ่ม 1-5: รับคำขอ → สแกน 3 Agent ที่ 09:00 → Equipment & CSSD ปฏิเสธเพราะชุดเข่าและหุ่นยนต์ยังอบอยู่ → CP-SAT เสนอ 12:30 → ล็อก → เคสฉุกเฉิน 12:15 แทรกและเลื่อนเคสอื่น
ทุกสถานะ เวลา และบรรทัด log มาจาก engine จริง และผ่านตัวตรวจอิสระก่อนเขียนลงไฟล์ ถ้าแก้ engine หรือสถานการณ์ ให้สร้างข้อมูลใหม่:

```bash
.venv/bin/python -m orchestra_engine.flow
```

Intake แบบ NLP/เสียงและการพยากรณ์เวลาจากประวัติยังไม่ได้ทำ ในหน้านี้คำขอเป็นข้อมูลโครงสร้าง

เวลาทั้งหมดเป็น **นาทีนับจากต้น horizon** (0 = 00:00 ของวันที่วางแผน) ใช้ `hm("08:30")` และ `fmt(510)` แปลงไปมา

## เงื่อนไขที่โมเดลบังคับ

**Hard:** ห้องไม่ซ้อน (รวมเวลาทำความสะอาด base/infectious) · บุคลากรไม่ซ้อน · เครื่องมือไม่ซ้อน (รวมเวลาอบ CSSD และ `available_at`) · เตียง PACU/ICU ไม่ซ้อน · อยู่ในกะ (`staff_schedules`) · ไม่เกิน `max_hours_per_week` · ห้องมี capability ตรง · ทีมมี role/skill ครบ · ทันเส้นตาย (deadline)

**Soft:** ลำดับความสำคัญ EMERGENCY > URGENT > ELECTIVE · ตรงเวลาที่แพทย์ต้องการ · จัดเคสเครื่องมือคล้ายกันติดกัน · ทีมที่เข้าขากัน (`team_synergy`) · ตอน reschedule เปลี่ยนเวลา/ห้อง/คน/เครื่องมือน้อยที่สุด

เคสที่จัดไม่ได้จะถูกรายงานพร้อมเหตุผล (เช่น ไม่มีห้อง HYBRID, พยาบาล ROBOTIC_CERTIFIED ไม่พอ, ไม่มีช่วงว่าง) และทุกเคสที่จัดได้มีบันทึก "why" ว่ารออะไร ใช้เขียนลง `ai_reasoning_log` ได้

## การจับคู่กับ Schema

| ตาราง | Engine |
|---|---|
| `operating_rooms` | `OperatingRoom` (`room_type` นับเป็น capability ด้วย) |
| `staff` + `staff_schedules` (REGULAR/ON_CALL ที่ `is_available`) | `StaffMember.shifts` |
| `equipment_units` + `equipment_types` | `EquipmentUnit` (`sterilization_duration_mins`, `available_at`) |
| `recovery_beds` | `RecoveryBed` |
| `surgery_requests` + `procedures` + `surgeon_preference_cards` | `SurgeryCase` |
| `team_synergy` | `team_synergy={(surgeon_id, staff_id): score}` |
| `scheduled_surgeries` + `resource_allocations` | `Assignment` (`cleanup_end_min`, `equipment_ready_min`) |

ค่าที่ schema ยังไม่มีคอลัมน์และ engine ใช้ค่า default: เวลาอยู่เตียงพักฟื้น (PACU 60 นาที / ICU 24 ชม.), เวลาเปิด-ปิดห้อง, skill ที่ต้องใช้ต่อ role

### Supabase CLI

ไฟล์ migration นี้คือ schema ที่รันใน Supabase ไปแล้ว หากเชื่อม repo กับโปรเจกต์ผ่าน Supabase CLI ให้ทำเครื่องหมายว่ารันแล้วก่อน `db push` เพื่อไม่ให้สร้างตารางซ้ำ:

```bash
supabase migration repair --status applied 20261031000000
```
