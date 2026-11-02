# คู่มือทีมพัฒนา ORchestraAI

คู่มือนี้สำหรับนักพัฒนาที่จะแก้หรือเพิ่มความสามารถของระบบ อธิบายโครงสร้างโค้ด วิธีตั้งเครื่อง workflow ตั้งแต่เริ่มงานจนขึ้นระบบจริง กติกาที่ต้องรักษา และสูตรงานที่ทำบ่อย

| เอกสาร | สำหรับ |
|---|---|
| `README.md` | ความสามารถของระบบทั้งหมด วิธีตั้งค่า Render / Supabase / LINE |
| หน้า `/help` ในระบบ | คู่มือผู้ใช้แต่ละบทบาท (ภาษาไทย พิมพ์ได้) |
| `docs/BACKLOG.md` | งานที่บันทึกไว้ทำภายหลัง |
| `/api/docs` (ต้องเข้าระบบ) | API ทุกเส้น สร้างจากโค้ดอัตโนมัติ (FastAPI / OpenAPI) |

---

## 1. ภาพรวมระบบ

ORchestraAI จัดตารางห้องผ่าตัดด้วย Agent หลายตัว ได้แก่ Staffing, Equipment & CSSD และ OR Facility ซึ่งตรวจทรัพยากรของตัวเอง ส่วน Master Orchestrator ใช้ **Google OR-Tools CP-SAT** หาเวลาที่ทุก Agent ผ่านพร้อมกันโดยไม่ชนกัน

- **Backend:** Python 3.12 + FastAPI (`app/`) และ engine ที่ไม่ขึ้นกับเว็บ (`orchestra_engine/`)
- **Frontend:** หน้า HTML ไฟล์เดียวต่อหน้า (`app/static/*.html`) เขียน CSS/JS ในไฟล์เอง ไม่มี build step และไม่มี framework
- **ฐานข้อมูล:** Supabase (PostgreSQL 16) โครงสร้างอยู่ใน `supabase/migrations/`
- **Hosting:** Render (`render.yaml`) deploy อัตโนมัติเมื่อ merge เข้า `main`
- **CI:** GitHub Actions (`.github/workflows/tests.yml`) รัน pyflakes และ pytest กับ PostgreSQL 16 จริง ทุก PR

### สองโหมดการทำงาน

| โหมด | เมื่อไร | เก็บข้อมูลที่ | คลาสหลัก |
|---|---|---|---|
| ข้อมูลจำลอง (demo) | ไม่ตั้ง `DATABASE_URL` | หน่วยความจำ (หายเมื่อเริ่มใหม่) มีวันเดียว | `ScheduleStore` (`app/store.py`) |
| Supabase | ตั้ง `DATABASE_URL` | PostgreSQL ทุกคำขออ่านสด | `DatabaseStore` (`app/db_store.py`) |

ความสามารถที่ใช้ได้ทั้งสองโหมดต้องมี method ชื่อเดียวกันในทั้งสอง store (เช่น `slot_check`, `find_slots`, `add_waiting`) ส่วนความสามารถที่ต้องมีฐานข้อมูล (ข้อมูลโรงพยาบาล, HIS, สำรองข้อมูล) เรียก `require_supabase` หรือตอบ 400 ในโหมด demo

### เส้นทางของคำขอหนึ่งครั้ง (ตัวอย่าง: จองเคส)

```
เบราว์เซอร์ (index.html) ── POST /api/bookings (JSON) ──▶ app/main.py
  middleware: จำกัดขนาด body (BodyLimit: ตาม Content-Length หรือนับขณะอ่านถ้าส่งแบบ chunked)
              → session cookie (หรือ API key ของ HIS ก่อนอ่าน body) → CSRF (ต้องเป็น JSON, origin เดียวกัน) → header ความปลอดภัย
  route: _user(request, "book") ตรวจสิทธิ์ตามบทบาท → _own_case (ศัลยแพทย์จองได้เฉพาะเคสตัวเอง)
  store.slot_check(...)  (DatabaseStore)
    _transaction(write=True): pg_advisory_xact_lock → เขียนได้ทีละคนทั้งระบบ
    db.load(conn, day) → World (ห้อง คน เครื่องมือ เตียง การจองของวันนั้น)
    engine: ORchestraOptimizer.check_slot → Agent 3 ตัวตรวจ → CP-SAT หาเวลาแรกที่ผ่าน
    _guard: ผลต้องไม่เพิ่มจุดขัดกันใหม่ (validate_schedule)
    db.save → บันทึก + กิจกรรม (ไม่มี HN) → exclusion constraint ใน DB เป็นด่านสุดท้าย
  commit แล้วจึงส่ง event → Notifier (มือถือ / LINE) ในเบื้องหลัง
  _state(...) → ส่งสถานะวันนั้นกลับ (_shape ซ่อน HN ตามบทบาท + บันทึกการเข้าถึงข้อมูลผู้ป่วย)
```

---

## 2. โครงสร้างโค้ด

```
orchestra_engine/        engine (ไม่รู้จักเว็บหรือฐานข้อมูล ทดสอบแยกได้)
  models.py              SurgeryCase, OperatingRoom, StaffMember, EquipmentUnit, RecoveryBed, Urgency …
  agents.py              Agent 3 ตัว (scan_slot) และ SlotCheck
  optimizer.py           ORchestraOptimizer: solve_schedule, check_slot, insert_emergency, handle_overrun, reschedule
  validation.py          validate_schedule (ตรวจทุกผลก่อนบันทึก)
  fatigue.py / cssd.py   เกณฑ์ความเหนื่อยล้า, คิว CSSD
  intake.py              พยากรณ์เวลาผ่าตัด, mask_identifiers (ปิด HN / ชื่อ / เลขบัตร)
  thai.py                ข้อความภาษาไทยของ engine
  demo.py, flow.py       ข้อมูลจำลอง และข้อมูลของหน้า web/orchestra-flow.html

app/
  main.py                FastAPI: middleware, ทุก route, การประกอบ store / notifier / งานเบื้องหลัง
  auth.py                บัญชี บทบาท PERMISSIONS session cookie การจำกัดการ login และขั้นตอนยืนยันตัวตน 2 ชั้น
  twofactor.py           รหัส 6 หลัก (TOTP, RFC 6238), รหัสสำรอง, QR code (segno)
  store.py               ScheduleStore (demo) + ตัวช่วยที่ใช้ร่วมกัน (build_case, StoreError, Activity …)
  db_store.py            DatabaseStore: งานทุกอย่างในโหมด Supabase (ธุรกรรม, outbox ของ event)
  db.py                  Database: load (World), save, pending, การตรวจคอลัมน์ที่มีจริง
  admin_store.py         ข้อมูลโรงพยาบาล (ห้อง บุคลากร เครื่องมือ หัตถการ card วัสดุ เวร)
  notify.py              แจ้งเตือน Web Push / LINE, หัวข้อ (KINDS) และข้อความ (build_messages)
  readiness.py           เช็กลิสต์ความพร้อม + DigestTimer (งานสรุปรายวัน)
  privacy.py             PDPA: บันทึกการเข้าถึง, ลบข้อมูลตามระยะเวลา (RetentionTimer 02:00)
  system_status.py       หน้า ⚙ สถานะระบบ: migration, ค่าตั้ง, เช็กลิสต์ go-live
  waitlist.py            หาวันว่างหลายวัน, รายการรอผ่าตัด
  feedback.py            ปุ่มแจ้งปัญหา + บันทึกข้อผิดพลาดของระบบ
  his.py                 เชื่อมต่อ HIS ของโรงพยาบาล (API ขาเข้า /api/his/v1/)
  backup.py              สำรอง / กู้คืนฐานข้อมูลทั้งหมด (ZIP) และถอดรหัสไฟล์ .orcbak
  autobackup.py          สำรองอัตโนมัติทุกคืน: เข้ารหัส AES-256-GCM, Supabase Storage (REST), BackupTimer
  *_io.py                นำเข้า / ส่งออก Excel (ข้อมูลโรงพยาบาล, เวร, ประวัติผ่าตัด)
  reports.py, blocks.py, cancellations.py, milestones.py, seed.py, intake_*.py
  static/                หน้าเว็บทั้งหมด + feedback.js (ใช้ร่วมกัน) + sw.js (Web Push)

supabase/migrations/     ไฟล์ SQL ตามลำดับเวลา (001 … 018)
tests/                   pytest (โหมด demo + โหมด Supabase กับ PostgreSQL จริง)
docs/                    คู่มือนี้ และ BACKLOG
```

---

## 3. ตั้งเครื่องสำหรับพัฒนา

**ต้องมี:** Python 3.12, PostgreSQL 16 (สำหรับเทสต์โหมด Supabase), Git และ Chromium + Playwright (ถ้าจะทดสอบในเบราว์เซอร์แบบอัตโนมัติ)

```bash
python3.12 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements-dev.txt
```

### รันแบบข้อมูลจำลอง (เร็วที่สุด)

ไม่ตั้ง `APP_PASSWORD` และ `DATABASE_URL` = โหมดพัฒนา เข้าเป็น admin โดยไม่ต้อง login (ใช้ในเครื่องตัวเองเท่านั้น)

```bash
.venv/bin/uvicorn app.main:app --reload --port 8000
```

เปิด `http://localhost:8000` · ถ้าตั้ง `APP_PASSWORD` จะต้อง login ด้วย `orchestra` / รหัสนั้น

### รันกับ PostgreSQL ในเครื่อง (เหมือนระบบจริง)

```bash
createdb orchestra_dev
```

```bash
for f in supabase/migrations/*.sql; do psql postgresql://postgres@localhost/orchestra_dev -q -f "$f"; done
```

```bash
DATABASE_URL=postgresql://postgres@localhost/orchestra_dev APP_PASSWORD=dev-password-123 READINESS_DIGEST_HOUR=off .venv/bin/uvicorn app.main:app --reload --port 8000
```

เข้าระบบด้วย `orchestra` / `dev-password-123` แล้วกด **ใส่ข้อมูลตัวอย่าง** บนหน้าตาราง (ฐานข้อมูลว่าง) · หรือรัน migration จากหน้า **⚙ สถานะระบบ** แทนคำสั่ง `psql` ก็ได้

### ดู engine อย่างเดียว

```bash
.venv/bin/python -m orchestra_engine.demo
```

---

## 4. ค่าตั้ง (Environment variables)

ระบบจริงตั้งที่ Render → service → **Environment** · หน้า ⚙ สถานะระบบบอกว่าตั้งแล้วหรือยัง (ไม่แสดงค่า)

| ชื่อ | ใช้ทำอะไร | ค่าตั้งต้น |
|---|---|---|
| `DATABASE_URL` | Supabase (Session pooler) · ไม่ตั้ง = โหมด demo | — |
| `APP_USERNAME` / `APP_PASSWORD` | บัญชีหลักของเจ้าของระบบ (admin) | `orchestra` / — |
| `SESSION_SECRET` | ลงลายมือชื่อ cookie การ login | ไม่ตั้ง = สร้างจาก `APP_PASSWORD` + `DATABASE_URL` (เปลี่ยนรหัสแล้วทุกคนต้อง login ใหม่) |
| `APP_TIMEZONE` | เขตเวลาของโรงพยาบาล | `Asia/Bangkok` |
| `SOLVER_WORKERS`, `SOLVER_TIME_LIMIT` | CPU และเวลาสูงสุดของ CP-SAT ต่อครั้ง | 2, 10 วินาที (Render ฟรี: 1, 15) |
| `ANTHROPIC_API_KEY`, `INTAKE_MODEL` | Intake Agent อ่านข้อความด้วย Claude · ไม่ตั้ง = กฎภาษาไทย | — |
| `LINE_CHANNEL_ACCESS_TOKEN`, `LINE_CHANNEL_SECRET`, `LINE_ADD_FRIEND_URL` | แจ้งเตือนทาง LINE | — |
| `VAPID_SUBJECT` | ผู้ส่ง Web Push | — |
| `APP_URL` | ที่อยู่เว็บในข้อความ LINE (Render ใช้ `RENDER_EXTERNAL_URL` ให้เอง) | — |
| `READINESS_DIGEST_HOUR` | ชั่วโมงส่งสรุปความพร้อมวันถัดไป + รายการรอผ่าตัด · `off` = ปิด | 14 |
| `HOSPITAL_NAME` | ชื่อโรงพยาบาลบนกระดาษพิมพ์ | — |

> **ห้าม** ใส่รหัสผ่าน connection string หรือ API key ลงในโค้ด commit แชต หรืออีเมล

---

## 5. Workflow: จากงานหนึ่งชิ้นจนขึ้นระบบจริง

```
1 รับงาน → 2 แตก branch → 3 เขียนโค้ด + เทสต์ → 4 ทดสอบในเบราว์เซอร์ → 5 commit + push
→ 6 เปิด PR → 7 CI ผ่าน + review → 8 merge → 9 Render deploy เอง → 10 รัน SQL ใหม่ (ถ้ามี) → 11 ตรวจหน้า ⚙ สถานะระบบ
```

1. **รับงาน:** เขียนให้ชัดว่าใครใช้ ใช้ตอนไหน และเสร็จแล้วหน้าตาเป็นอย่างไร งานใหญ่ให้แบ่งเป็นชิ้นที่ merge ได้ทีละชิ้น
2. **แตก branch จาก `main` ล่าสุด:**
   ```bash
   git fetch origin main && git checkout -b feature/waitlist-sms origin/main
   ```
   ตั้งชื่อ `feature/…`, `fix/…`, `docs/…` · หนึ่ง branch ต่อหนึ่งเรื่อง
3. **เขียนโค้ดพร้อมเทสต์:**
   - เทสต์ใหม่อยู่ใน `tests/test_<เรื่อง>.py`
   - ก่อน commit ต้องผ่านทั้ง 2 คำสั่ง:
   ```bash
   .venv/bin/python -m pyflakes app orchestra_engine
   ```
   ```bash
   TEST_DATABASE_URL=postgresql://postgres@localhost/postgres .venv/bin/python -m pytest -q
   ```
4. **ทดสอบในเบราว์เซอร์:** เปิดหน้าที่แก้ทั้งโหมด demo และโหมดฐานข้อมูล
   - ลองทุกปุ่มที่แก้
   - ดู console ว่าไม่มี error
   - ลองบนจอมือถือ (กว้าง 390px) ว่าไม่มีแถบเลื่อนแนวนอน
   - ถ้าแก้ migration ให้ลองทั้ง "ก่อนรัน" และ "หลังรัน SQL"
5. **commit:**
   - ข้อความบรรทัดแรกบอกว่าได้อะไร (ภาษาอังกฤษ ไม่เกิน ~72 ตัวอักษร) บรรทัดถัดไปบอกรายละเอียดและเหตุผล
   - push เฉพาะ branch ของตัวเอง ห้าม push ตรงเข้า `main` และห้าม force push branch ที่คนอื่นใช้
6. **เปิด PR เข้า `main`:** ในคำอธิบาย PR ให้มี:
   - **Summary** (ทำอะไร เพราะอะไร)
   - **Test plan** (เทสต์ที่เพิ่ม ผลรันชุดเต็ม และสิ่งที่ลองในเบราว์เซอร์)
   - **สิ่งที่ต้องทำหลัง merge** เช่น ไฟล์ SQL ใหม่ หรือค่าตั้งใหม่
7. **CI ต้องเขียว** (check `pytest (Python 3.12, PostgreSQL 16)`) และมีคนในทีม review อย่างน้อย 1 คน
   - ถ้า CI แดง ให้ดู log หาสาเหตุจริงแล้วแก้
   - **ห้าม** ปิดหรือข้ามเทสต์เพื่อให้เขียว
   - แนะนำ: ตั้ง branch protection ให้ `main` ต้องผ่าน check นี้ก่อน merge
8. **merge:** ใช้แบบ merge commit ตามที่ใช้อยู่
9. **Render deploy เอง:** ใช้เวลา 2–5 นาที · เวอร์ชันที่ขึ้นจริงดูได้ที่หน้า ⚙ สถานะระบบ (7 ตัวแรกของ commit)
10. **รัน SQL ใหม่:** ⚙ สถานะระบบ → **รัน SQL ที่ยังขาด** → **รันเลย** (หรือวางใน Supabase SQL Editor) · โค้ดใหม่ต้องทำงานได้แม้ยังไม่รัน (ดูข้อ 6)
11. **ตรวจหลังขึ้นระบบ:**
    - หน้า ⚙ สถานะระบบ: เช็กลิสต์ไม่มีสีแดงใหม่ และหัวข้อ "ข้อผิดพลาดของระบบ" ไม่มีเรื่องใหม่
    - ลองใช้ความสามารถที่เพิ่งขึ้นจริง 1 ครั้ง

**ถ้าระบบจริงมีปัญหาหลัง deploy:**
1. Render → service → **Events** → เลือก deploy ก่อนหน้า → **Rollback**
2. แก้ใน branch ใหม่ แล้วทำ workflow เดิม

migration ย้อนไม่ได้ จึงต้องเพิ่มอย่างเดียว ไม่ลบหรือเปลี่ยนชื่อคอลัมน์ที่โค้ดเวอร์ชันเก่ายังใช้

---

## 6. ฐานข้อมูลและ migration

**ชื่อไฟล์:** `supabase/migrations/YYYYMMDDHHMMSS_<เรื่อง>.sql` เรียงตามเวลา และเลขลำดับ (001, 002, …) คือลำดับของไฟล์

**กติกาของไฟล์ SQL ใหม่:**
- **รันซ้ำได้:** ใช้ `CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS` และครอบด้วย `BEGIN; … COMMIT;`
- **เปิด RLS ทุกตารางใหม่:** `ALTER TABLE … ENABLE ROW LEVEL SECURITY;` โดยไม่สร้าง policy เพื่อให้มีแค่เซิร์ฟเวอร์ของเราที่อ่านได้ ไม่ใช่ Supabase anon key
- **เพิ่มอย่างเดียว:** ไม่ลบตารางหรือคอลัมน์ ไม่เปลี่ยนชนิดข้อมูลแบบทำลายข้อมูล
- **บรรทัดแรกเป็น comment** `-- ORchestraAI migration NNN: <เรื่อง>` และบอกว่าระบบทำงานอย่างไรถ้ายังไม่รัน

**โค้ดต้องทำงานได้ก่อนรัน SQL ใหม่:** ตรวจตอนใช้งานว่ามีตารางหรือคอลัมน์หรือยัง ด้วย `to_regclass(...)`, `information_schema.columns` หรือ `World.has(table, column)` แล้วปิดเฉพาะความสามารถนั้น พร้อมข้อความว่า "ต้องรัน SQL ไฟล์ NNN ก่อน"

**Checklist เมื่อเพิ่มไฟล์ SQL:**
- [ ] เพิ่มชื่อภาษาไทยใน `TITLES` ของ `app/system_status.py`
- [ ] เทสต์ `test_works_before_migration_NNN`: drop ส่วนที่ไฟล์สร้าง แล้วตรวจว่าหน้าเดิมยังใช้ได้
- [ ] รายการไฟล์ SQL ใน `README.md` (หัวข้อเชื่อม Supabase) และตารางไฟล์ด้านบนของ README
- [ ] ตารางใหม่ที่มีข้อมูลผู้ป่วยต้องเข้ากับการลบตามระยะเวลา PDPA (`app/privacy.py`)
- [ ] สำรองและกู้คืน (`app/backup.py`) ทำงานกับทุกตารางเองอยู่แล้ว แต่ต้องตรวจว่าลำดับ foreign key ยังถูก (`tests/test_backup.py`)

**การเขียน SQL ใน Python (psycopg 3):**
- ใช้ parameter `%s` เสมอ ห้ามต่อสตริงค่าจากผู้ใช้
- ชื่อตารางหรือคอลัมน์ที่ต้องประกอบเองต้องมาจากรายการคงที่ในโค้ด และใส่ `# noqa: S608` พร้อมเหตุผล
- query ที่ **ไม่มี** parameter ส่งไปตามตัวอักษร จึงห้ามเขียน `%%`
- query ที่ **มี** parameter ต้องเขียน `%` ในข้อความเป็น `%%` (เช่น `LIKE 'HIST-%%'`)
- งานเขียนข้อมูลทำใน `store._transaction(write=True)` เท่านั้น ซึ่งล็อกการเขียนทั้งระบบและส่ง event หลัง commit
- งานอ่านใช้ `write=False`

---

## 7. เทสต์

```bash
.venv/bin/python -m pytest -q
```

```bash
TEST_DATABASE_URL=postgresql://postgres@localhost/postgres .venv/bin/python -m pytest -q
```

- **ถ้าไม่ตั้ง `TEST_DATABASE_URL`:** เทสต์โหมด Supabase ถูกข้าม
- **ถ้าตั้ง:** แต่ละเทสต์สร้างฐานข้อมูลใหม่ รันทุกไฟล์ SQL ใส่ข้อมูลตัวอย่าง แล้วลบทิ้ง บัญชีที่ใช้จึงต้องสร้างฐานข้อมูลได้
- ชุดเต็มใช้เวลาประมาณ 4–5 นาที ระหว่างพัฒนารันเฉพาะไฟล์ได้ เช่น `pytest -q tests/test_waitlist.py`

**fixture ที่ใช้บ่อย:**

| fixture / ตัวช่วย | อยู่ที่ | ให้อะไร |
|---|---|---|
| `fresh` (autouse), `owner`, `make_user(owner, ชื่อ, บทบาท)` | `tests/test_app.py` | แอปโหมด demo ที่รีเซ็ตแล้ว, client ที่ login เป็น admin, ผู้ใช้บทบาทต่าง ๆ |
| `db_url`, `store` | `tests/test_db.py` | ฐานข้อมูลใหม่ + `DatabaseStore` ที่ใส่ข้อมูลตัวอย่างแล้ว |
| `_request(state, แพทย์, หัตถการ, HN)`, `_tomorrow(store)`, `_rows(url, sql)` | `tests/test_db.py` | คำขอจองจาก preference card, วันพรุ่งนี้, อ่านแถวจาก DB |

**เทสต์ที่คุมคุณภาพทั้งระบบ:**
- `test_thai_messages.py`: ข้อความที่ผู้ใช้เห็นต้องเป็นภาษาไทย
- `test_help.py`: ทุกปุ่ม (`<kbd>`) ที่คู่มือ `/help` อ้างถึงต้องมีจริงในหน้าเว็บ ถ้าเปลี่ยนชื่อปุ่มต้องแก้คู่มือด้วย
- `test_security.py`: header ความปลอดภัยและขนาดคำขอ, การจำกัดการ login, การออกจากระบบ, สิทธิ์ตามบทบาท, ไฟล์ Excel/CSV, ปลายทางแจ้งเตือน
- `test_backup.py`: สำรองแล้วกู้คืนได้ครบทุกตาราง
- `test_autobackup.py`: เข้ารหัส/ถอดรหัส, Supabase Storage ปลอมบน 127.0.0.1, ลบไฟล์เก่า, ตั้งเวลา, API

**ระวังเทสต์ที่ขึ้นกับเวลา:**
- ข้อมูลตัวอย่างเป็น "วันนี้" ตามนาฬิกาจริง ตอนเย็นเคสของวันนี้จึงจบหมด
- เทสต์ที่ต้องการเคสที่ยังไม่เริ่มให้ใช้ `_tomorrow(store)`
- ถ้าเทสต์ผ่านตอนเช้าแต่ตก CI ตอนเย็น สาเหตุมักเป็นเรื่องนี้

---

## 8. กติกาที่ต้องรักษา

### ภาษาและหน้าเว็บ
- **ข้อความที่ผู้ใช้เห็นเป็นภาษาไทยทั้งหมด:** ทั้งข้อความผิดพลาดจาก `StoreError`, ข้อความแจ้งเตือน และข้อความใน `thai.py` ส่วนชื่อตัวแปรและ comment ในโค้ดเป็นภาษาอังกฤษ
- **ทุกหน้าเป็นไฟล์เดียว:** CSS/JS อยู่ในไฟล์ (CSP อนุญาตเฉพาะ `'self'` และ Google Fonts) โหลด `/feedback.js` ท้าย `<body>` เพื่อให้มีปุ่มแจ้งปัญหา
- **ใช้สีจากตัวแปร:** ใช้ `var(--accent)`, `--ok`, `--bad`, `--warn` … เพื่อให้ dark mode ทำงาน
- **POST/PUT/PATCH/DELETE ต้องส่ง JSON:** ตัวช่วย `api()` ในแต่ละหน้าทำให้แล้ว ถ้าไม่ใช่ JSON จะได้ 415 (กัน CSRF)
- **รองรับมือถือ:** ต้องใช้ได้ที่จอกว้าง 390px และไม่มีแถบเลื่อนแนวนอน

### ข้อมูลผู้ป่วย (PDPA)
- **แจ้งเตือน (มือถือ / LINE) และบันทึกกิจกรรม:** ห้ามมี HN หรือชื่อผู้ป่วย ใช้รหัสเคส (เช่น `R-1A2B3C`) แทน
- **ข้อความอิสระที่คนพิมพ์และระบบเก็บ** (แจ้งปัญหา, หมายเหตุ): ส่งผ่าน `mask_identifiers` ก่อนเก็บหรือก่อนส่งออกนอกระบบ
- **ส่งข้อมูลผู้ป่วยกลับให้ใคร** (หน้าตาราง, พิมพ์, HIS อ่าน, สำรองข้อมูล): บันทึกด้วย `access.record(...)` (`app/privacy.py`)
- **บทบาทที่ไม่มีสิทธิ์ `patients`** (เช่น CSSD): ต้องไม่เห็น HN `_shape()` ใน `main.py` ทำให้แล้ว ถ้าส่งข้อมูลใหม่ให้หน้าเว็บ ต้องตรวจว่าผ่านจุดนี้
- **เก็บข้อมูลผู้ป่วยเท่าที่จำเป็น** และกำหนดว่าจะลบเมื่อไร · การลบตามรอบอยู่ใน `privacy.apply` (รันทุกคืน 02:00) เพิ่มตารางใหม่ที่เก็บข้อความอิสระที่นั่น
- **ข้อมูลจากภายนอกที่ระบุตัวผู้ป่วยได้ทางอ้อม** (เช่นเลขที่ใบขอของ HIS): เมื่อลบข้อมูลระบุตัวตนของเคส ต้องลบหรือ hash ด้วย

### ความปลอดภัย
- **ตรวจสิทธิ์ที่ API เสมอ:** ใช้ `_user(request, "<permission>")` หรือ `_admin(...)` ตาม `PERMISSIONS` ใน `app/auth.py` การซ่อนปุ่มในหน้าเว็บไม่นับเป็นการกันสิทธิ์
- **ศัลยแพทย์จัดการได้เฉพาะเคสของตัวเอง:** ใช้ `check_own_case` / `_own_case`
- **กำหนดขอบเขตข้อมูลขาเข้า:** ใช้ pydantic `Field(max_length=…, ge=…, le=…)` ทุกช่อง
- **key, token และรหัสผ่านเก็บเป็น hash เท่านั้น:** ดูตัวอย่างที่ `his._hash` (ข้อยกเว้น: รหัสลับ TOTP ต้องเก็บแบบอ่านได้ เพราะใช้คำนวณรหัส จึงต้องรักษาไฟล์สำรองข้อมูลให้ดี)
- **endpoint ใหม่กับบัญชีที่ต้องตั้งยืนยันตัวตน 2 ชั้น:** middleware ปิดทุก API ยกเว้น `ALLOWED_WHILE_2FA_SETUP` และพาหน้าเว็บไปที่ `/security` · ถ้าหน้าใหม่ต้องเปิดได้ระหว่างนั้น เพิ่มใน `PAGES_WHILE_2FA_SETUP`
- **token ที่ใช้ชั่วคราว** (ระหว่างรหัสผ่านกับรหัส 6 หลัก, ตั้งค่า QR) เซ็นด้วยกุญแจแยกจาก session (`Accounts.pending_signer` / `setup_signer`) จึงใช้แทน session ไม่ได้ · ทำแบบเดียวกันเมื่อเพิ่ม token ชนิดใหม่
- **ห้าม log ค่าลับหรือข้อมูลผู้ป่วย:** ข้อผิดพลาดที่ log ด้วย logger `orchestra.*` ระดับ ERROR จะไปขึ้นหน้า ⚙ สถานะระบบด้วย (ผ่าน `mask_identifiers` และเก็บเฉพาะบรรทัดแรกของ exception ด้วย `feedback.describe` เพราะ error ของฐานข้อมูลมีแถวข้อมูลผู้ป่วยในบรรทัดถัดไป)
- **regex กับข้อความที่ผู้ใช้ส่งมา:** ต้องใช้เวลาเชิงเส้น ห้ามมี `\s*` สองตัวติดกันโดยมีตัวเลือกคั่น (เช่น `\s*-?\s*`) หรือ `\(.*?\)` (ใช้ `\([^()]*\)`) และตัดความยาวข้อความก่อน · Python `re` ถือ GIL ทั้งเซิร์ฟเวอร์จะหยุดรอ · มีเทสต์จับเวลาใน `test_intake.py`
- **ขนาดข้อมูลขาเข้า:** ปรับ `BODY_LIMITS` ใน `main.py` เมื่อเพิ่ม endpoint ที่ไม่ต้องรับไฟล์ · error 422 ไม่ส่งค่าที่ได้รับกลับไป
- **งานหนักที่ผู้ใช้สั่งได้** (เช่นค้นหาวันว่าง): จำกัดจำนวนที่ทำพร้อมกัน (`SEARCHES`) และทีละรายการต่อคน

### การออกแบบ
- **ทำงานได้ทั้งสองโหมด:** ความสามารถใหม่ต้องใช้ได้ในโหมด demo หรือตอบชัดเจนว่าต้องใช้ Supabase
- **ทำงานได้ก่อนรัน SQL:** ดูข้อ 6
- **ตารางผ่าตัดต้องไม่ชนกัน:** ผลจาก engine ต้องผ่าน `validate_schedule` / `_guard` ก่อนบันทึกทุกครั้ง ห้ามเขียน `scheduled_surgeries` ตรงโดยไม่ผ่าน `db.save`
- **ระบบภายนอกห้ามเปลี่ยนเคสที่จองแล้วเอง:** เช่น HIS ให้แจ้งคนแทน
- **งานเบื้องหลังต้องไม่ล้มทั้งเซิร์ฟเวอร์:** ครอบ `try/except` แล้ว `log.exception(...)` และใช้ `claim_digest` / `app_settings` กันการทำซ้ำเมื่อมีหลายเครื่องหรือเริ่มใหม่

---

## 9. สูตรงานที่ทำบ่อย

### เพิ่ม API ใหม่
1. **ตรรกะ:** เขียน method ใน store (ทั้ง `DatabaseStore` และ `ScheduleStore` ถ้าใช้ได้ทั้งสองโหมด) หรือในโมดูลของเรื่องนั้น โดยโยน `StoreError` พร้อมข้อความไทยเมื่อข้อมูลไม่ถูกต้อง
2. **route ใน `app/main.py`:**
   - กำหนด pydantic model ของ body
   - ตรวจสิทธิ์ด้วย `_user` / `_admin`
   - เรียกผ่าน `_run(...)` ซึ่งแปลง `StoreError` เป็น 400 และ `Forbidden` เป็น 403
3. **ถ้าหน้าเว็บต้องเห็นผลทันที:** คืน `{"result": …, "state": _state(user, date, request)}`
4. **เทสต์:**
   - ทางที่ถูกต้อง
   - ข้อมูลผิด (400 / 422)
   - บทบาทที่ไม่มีสิทธิ์ (403)
   - ถ้าเกี่ยวกับ migration: ก่อนรัน SQL

### เพิ่มแท็บในหน้าข้อมูลโรงพยาบาล (`admin.html`)
1. **ปุ่มแท็บ:** เพิ่ม `<button class="tab" data-tab="ชื่อ" data-perm="สิทธิ์">`
2. **เนื้อหา:** เพิ่ม `<section class="card" data-panel="ชื่อ" hidden>`
3. **ฟังก์ชันโหลด:** เขียน `loadชื่อ()` แล้วเพิ่มใน map ของ `selectTab` ลิงก์ `/admin#ชื่อ` จะเปิดแท็บนั้นเอง

### เพิ่มหัวข้อแจ้งเตือนใหม่
1. **ใน `app/notify.py`:**
   - เพิ่มหัวข้อใน `KINDS` (ข้อความไทย) และ `KIND_ROLES` (บทบาทที่ได้รับ)
   - เพิ่ม `elif event["type"] == ...` ใน `build_messages`
2. **ส่ง event:** ส่งจาก store ด้วย `self._emit(event)` (ส่งหลัง commit) หรือส่งให้ `notifier.handle`
3. **เทสต์:**
   - แก้ค่าที่คาดไว้ของ prefs ใน `tests/test_notify.py`
   - เพิ่มเทสต์ว่าข้อความไม่มี HN

### เพิ่มงานตั้งเวลารายวัน
- ดูตัวอย่าง `DigestTimer` (`app/readiness.py`) และ `RetentionTimer` (`app/privacy.py`) โดยเริ่มใน `_lifespan` ของ `main.py`
- **ทำครั้งเดียวต่อวัน:** ใช้ `claim_digest(day, kind=...)`
- **ให้หน้าสถานะเห็นว่างานทำงาน:** เรียก `mark_job(...)`
- **Render แผนฟรีหลับเมื่อไม่มีคนใช้:** งานจะทำเมื่อเซิร์ฟเวอร์ตื่นครั้งถัดไป ต้องออกแบบให้ทำย้อนหลังได้

### เชื่อม HIS แบบดึงข้อมูล (เมื่อโรงพยาบาลมี API ให้เราเรียกได้)
1. **ตัวเชื่อม:** เขียนไฟล์ใหม่ เช่น `app/his_<ยี่ห้อ>.py` ให้อ่านข้อมูลจาก API ของโรงพยาบาล แล้วแปลงเป็น dict รูปแบบเดียวกับ `HisRequestIn`
2. **ส่งเข้าระบบ:** ส่งเข้า `his.receive(payload, source="<ยี่ห้อ>")` เพื่อให้การจับคู่รหัส กล่องรับข้อมูล PDPA และการกันเคสที่จองแล้ว ทำงานเหมือนเดิมทั้งหมด
3. **ตั้งเวลา:** เรียกตามรอบด้วยงานตั้งเวลา (หัวข้อก่อนหน้า)

### เปลี่ยนชื่อปุ่มหรือข้อความบนหน้าเว็บ
- ค้นใน `app/static/help.html` ว่าคู่มืออ้างชื่อเดิมไหม (`test_help.py` จะตกถ้าลืม) และค้นใน `tests/` ว่ามีเทสต์ตรวจข้อความนั้นหรือไม่

---

## 10. ดูแลระบบจริง

- **หน้า ⚙ สถานะระบบ:**
  - เช็กลิสต์ go-live
  - ไฟล์ SQL
  - การเริ่มใหม่ของเซิร์ฟเวอร์
  - งานตั้งเวลา
  - สำรองข้อมูล
  - เรื่องที่ผู้ใช้แจ้ง
  - ข้อผิดพลาดของระบบ
  - การเชื่อมต่อ HIS
- **สำรองข้อมูล:** Supabase แผนฟรีไม่สำรองให้
  - ตั้งสำรองอัตโนมัติทุกคืน (`SUPABASE_SERVICE_ROLE_KEY`, `BACKUP_ENCRYPTION_KEY`) แล้วดูผลที่หน้าสถานะระบบทุกเช้า
  - หรือปุ่มดาวน์โหลดที่หน้าสถานะระบบ / คำสั่งด้านล่าง อย่างน้อยสัปดาห์ละครั้ง
  - ทดสอบกู้คืนจากไฟล์ `.orcbak` ลงโปรเจกต์ Supabase ทดสอบอย่างน้อยไตรมาสละครั้ง (ไฟล์ที่ไม่เคยลองกู้ = ยังไม่รู้ว่าใช้ได้)
  - ไฟล์มีข้อมูลผู้ป่วย ต้องเก็บเหมือนเวชระเบียน
  ```bash
  DATABASE_URL=<ใส่ในเครื่องเท่านั้น> .venv/bin/python -m app.backup dump backup.zip
  ```
- **Log ของเซิร์ฟเวอร์:** Render → service → **Logs**
- **ข้อผิดพลาดที่ไม่คาดคิด:** ระบบเก็บให้เองในหน้าสถานะระบบ (หัวข้อข้อผิดพลาดของระบบ) และแจ้ง admin
- **Supabase แผนฟรีหยุดโปรเจกต์เมื่อไม่ได้ใช้นาน:**
  - หน้าเว็บจะขึ้น "เชื่อมต่อฐานข้อมูลไม่ได้"
  - เข้า Supabase dashboard แล้วกด Restore
- **API docs:** `/api/docs` (ต้อง login) สำหรับดู API และลองเรียก

---

## 11. ปัญหาที่เจอบ่อย

| อาการ | สาเหตุ / วิธีแก้ |
|---|---|
| POST ได้ 415 "ต้องส่งข้อมูลแบบ JSON" | ต้องส่ง `Content-Type: application/json` (กัน CSRF) รวมถึง DELETE ที่ไม่มี body: ส่ง `{}` |
| query พังเพราะ `%` | ดูข้อ 6: ไม่มี parameter = ใช้ `%` ตรง ๆ, มี parameter = `%%` |
| เทสต์ผ่านในเครื่องแต่ตก CI (หรือกลับกัน) ตอนเย็น | เทสต์ขึ้นกับเวลาของวัน ใช้วันพรุ่งนี้ (`_tomorrow`) แทนวันนี้ |
| เทสต์ฐานข้อมูลขึ้น `Connection refused` | PostgreSQL ในเครื่องหยุด สั่ง start ใหม่ |
| หน้าเว็บโหลดไม่ขึ้นหลังแก้ JS | เปิด console: ตัวแปรชื่อซ้ำกับที่มีอยู่ในหน้า (`const` ประกาศซ้ำ) หรือ class CSS ชนกัน |
| หลัง deploy บางความสามารถขึ้น "ต้องรัน SQL ไฟล์ …" | ยังไม่ได้รัน migration ใหม่: หน้า ⚙ สถานะระบบ → รัน SQL ที่ยังขาด |
| ผู้ใช้ไม่ได้รับแจ้งเตือน | ผู้ใช้ต้องเปิดแจ้งเตือนบนอุปกรณ์ของตัวเอง (🔔), หัวข้อนั้นต้องเปิดอยู่, และคนที่ทำรายการเองจะไม่ได้รับ |
