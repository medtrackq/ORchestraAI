-- เปิดใช้งาน Extension สำหรับสร้าง UUID และป้องกันการจองเวลาชนกัน
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ==========================================
-- MODULE 1: STAFF & ROSTER (สำหรับ Staffing Agent)
-- ==========================================
CREATE TABLE staff (
    staff_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    employee_code VARCHAR(50) UNIQUE NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    role VARCHAR(50) NOT NULL, -- 'SURGEON', 'ANESTHESIOLOGIST', 'SCRUB_NURSE', 'CIRCULATING_NURSE'
    specialty VARCHAR(100),    -- เช่น 'ORTHOPEDICS', 'NEURO', 'CARDIO'
    skills JSONB DEFAULT '[]', -- เช่น ["ROBOTIC_CERTIFIED", "PEDIATRIC_CARE"]
    max_hours_per_week INT DEFAULT 48,
    current_weekly_hours NUMERIC(5,2) DEFAULT 0.0, -- AI ใช้เช็กความเหนื่อยล้า (Fatigue)
    is_active BOOLEAN DEFAULT TRUE
);

CREATE TABLE staff_schedules (
    schedule_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    staff_id UUID REFERENCES staff(staff_id) ON DELETE CASCADE,
    shift_start TIMESTAMPTZ NOT NULL,
    shift_end TIMESTAMPTZ NOT NULL,
    shift_type VARCHAR(30) DEFAULT 'REGULAR', -- 'REGULAR', 'ON_CALL', 'LEAVE'
    is_available BOOLEAN DEFAULT TRUE
);

-- เก็บความเข้าขากันของทีม (Soft Constraint: ทีมที่คุ้นเคยกันจะผ่าตัดเร็วขึ้น)
CREATE TABLE team_synergy (
    surgeon_id UUID REFERENCES staff(staff_id),
    staff_id UUID REFERENCES staff(staff_id),
    co_working_count INT DEFAULT 1,
    synergy_score NUMERIC(3,2) DEFAULT 1.00, -- คะแนน 0.00 - 5.00
    PRIMARY KEY (surgeon_id, staff_id)
);

-- ==========================================
-- MODULE 2: EQUIPMENT & CSSD (สำหรับ Equipment Agent)
-- ==========================================
CREATE TABLE equipment_types (
    eq_type_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(150) NOT NULL,         -- เช่น 'Robotic Arm Da Vinci', 'C-Arm', 'Ortho Knee Set'
    category VARCHAR(50) NOT NULL,     -- 'HEAVY_MACHINE', 'INSTRUMENT_TRAY'
    requires_sterilization BOOLEAN DEFAULT TRUE,
    sterilization_duration_mins INT DEFAULT 120 -- เวลาส่งล้างและอบฆ่าเชื้อที่ CSSD
);

CREATE TABLE equipment_units (
    unit_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    eq_type_id UUID REFERENCES equipment_types(eq_type_id),
    serial_number VARCHAR(100) UNIQUE NOT NULL,
    current_status VARCHAR(30) DEFAULT 'AVAILABLE', -- 'AVAILABLE', 'IN_USE', 'STERILIZING', 'MAINTENANCE'
    current_location VARCHAR(100),                  -- เช่น 'OR-3', 'CSSD_ZONE', 'FLOOR_4'
    available_at TIMESTAMPTZ DEFAULT NOW()          -- เวลาที่เครื่องมือจะพร้อมใช้งาน (AI ใช้ดูคิวอบฆ่าเชื้อ)
);

-- ==========================================
-- MODULE 3: OR ROOMS & ICU/PACU (สำหรับ OR Facility Agent)
-- ==========================================
CREATE TABLE operating_rooms (
    or_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    room_code VARCHAR(20) UNIQUE NOT NULL, -- เช่น 'OR-1', 'OR-2', 'OR-3'
    room_type VARCHAR(50) NOT NULL,        -- 'STANDARD', 'HYBRID', 'NEGATIVE_PRESSURE'
    capabilities JSONB DEFAULT '[]',       -- เช่น ["ROBOTIC_READY", "LEAD_LINED_XRAY", "LAMINAR_FLOW"]
    base_turnover_mins INT DEFAULT 30,     -- เวลาทำความสะอาดห้องมาตรฐาน
    infectious_turnover_mins INT DEFAULT 60, -- เวลาทำความสะอาดกรณีเคสติดเชื้อ (Dirty Case)
    status VARCHAR(30) DEFAULT 'AVAILABLE'
);

CREATE TABLE recovery_beds (
    bed_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    bed_code VARCHAR(20) UNIQUE NOT NULL,
    bed_type VARCHAR(20) NOT NULL, -- 'PACU', 'ICU_SURGICAL'
    is_occupied BOOLEAN DEFAULT FALSE,
    available_from TIMESTAMPTZ DEFAULT NOW()
);

-- ==========================================
-- MODULE 4: PROCEDURES & PREFERENCE CARDS (สำหรับ Intake Agent)
-- ==========================================
CREATE TABLE procedures (
    procedure_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    icd9_cm_code VARCHAR(20) UNIQUE,
    name VARCHAR(200) NOT NULL,            -- เช่น 'Total Knee Arthroplasty (TKA)'
    default_duration_mins INT NOT NULL,    -- เวลาเฉลี่ยมาตรฐาน
    required_room_capabilities JSONB DEFAULT '[]',
    requires_icu_post_op BOOLEAN DEFAULT FALSE,
    infection_risk_level VARCHAR(20) DEFAULT 'CLEAN' -- 'CLEAN', 'CONTAMINATED', 'DIRTY'
);

-- หัวใจสำคัญ: แม่แบบความต้องการเฉพาะตัวของศัลยแพทย์แต่ละท่าน
CREATE TABLE surgeon_preference_cards (
    card_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    surgeon_id UUID REFERENCES staff(staff_id),
    procedure_id UUID REFERENCES procedures(procedure_id),
    historical_avg_duration_mins INT NOT NULL, -- เวลาผ่าตัดจริงที่ AI คำนวณได้จากประวัติหมอคนนี้
    historical_stddev_mins INT DEFAULT 15,     -- ความคลาดเคลื่อนของเวลา
    required_staff_roles JSONB NOT NULL,       -- เช่น {"ANESTHESIOLOGIST": 1, "SCRUB_NURSE": 2}
    required_equipment_types JSONB NOT NULL,   -- รายการ eq_type_id ที่หมอคนนี้ต้องใช้เสมอ
    special_notes TEXT,
    UNIQUE(surgeon_id, procedure_id)
);

-- ==========================================
-- MODULE 5: REQUESTS & MASTER SCHEDULE (สำหรับ Master Orchestrator)
-- ==========================================
CREATE TABLE surgery_requests (
    request_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    patient_hn VARCHAR(50) NOT NULL,
    patient_name VARCHAR(150) NOT NULL,
    primary_surgeon_id UUID REFERENCES staff(staff_id),
    procedure_id UUID REFERENCES procedures(procedure_id),
    urgency_level VARCHAR(20) NOT NULL, -- 'ELECTIVE', 'URGENT', 'EMERGENCY'
    raw_nlp_input TEXT,                 -- ข้อความ/เสียงต้นฉบับที่หมอสั่งผ่าน Intake Agent
    ai_predicted_duration_mins INT NOT NULL, -- เวลาที่ AI ประเมินให้
    preferred_date_start DATE,
    preferred_date_end DATE,
    status VARCHAR(30) DEFAULT 'PENDING', -- 'PENDING', 'SCHEDULED', 'RESCHEDULED', 'CANCELLED'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE scheduled_surgeries (
    schedule_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    request_id UUID REFERENCES surgery_requests(request_id),
    or_id UUID REFERENCES operating_rooms(or_id),
    reserved_bed_id UUID REFERENCES recovery_beds(bed_id),
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    cleanup_end_time TIMESTAMPTZ NOT NULL, -- เวลาที่ห้องทำความสะอาดเสร็จพร้อมรับเคสต่อไป
    actual_start_time TIMESTAMPTZ,         -- เก็บเวลาจริงเพื่อนำไปเทรน AI รอบถัดไป
    actual_end_time TIMESTAMPTZ,
    ai_optimization_score NUMERIC(5,2),    -- คะแนนความเหมาะสมของสล็อตนี้
    ai_reasoning_log TEXT,                 -- เหตุผลที่ AI เลือกสล็อตนี้ (Explainable AI)
    status VARCHAR(30) DEFAULT 'CONFIRMED',
    -- Hard Constraint ระดับ DB: ห้ามจองห้องผ่าตัดเดียวกันในเวลาที่ทับซ้อนกันเด็ดขาด
    EXCLUDE USING gist (or_id WITH =, tstzrange(start_time, cleanup_end_time) WITH &&)
);

-- ตารางผูกทรัพยากร (คน + เครื่องมือ) เข้ากับตารางผ่าตัด
CREATE TABLE resource_allocations (
    allocation_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    schedule_id UUID REFERENCES scheduled_surgeries(schedule_id) ON DELETE CASCADE,
    resource_type VARCHAR(20) NOT NULL, -- 'STAFF' หรือ 'EQUIPMENT'
    staff_id UUID REFERENCES staff(staff_id),
    equipment_unit_id UUID REFERENCES equipment_units(unit_id),
    allocated_from TIMESTAMPTZ NOT NULL,
    allocated_until TIMESTAMPTZ NOT NULL,
    -- Hard Constraint ระดับ DB: ห้ามบุคลากรคนเดิมถูกจัดเข้า 2 ห้องในเวลาที่ทับซ้อนกัน
    EXCLUDE USING gist (staff_id WITH =, tstzrange(allocated_from, allocated_until) WITH &&) WHERE (staff_id IS NOT NULL),
    -- Hard Constraint ระดับ DB: ห้ามเครื่องมือชิ้นเดิมถูกใช้ซ้อนกัน
    EXCLUDE USING gist (equipment_unit_id WITH =, tstzrange(allocated_from, allocated_until) WITH &&) WHERE (equipment_unit_id IS NOT NULL)
);