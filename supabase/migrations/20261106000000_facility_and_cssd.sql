-- ORchestraAI migration 007: OR Facility Agent (elective hours, recovery beds, recovery time)
-- and Equipment & CSSD Agent (planned maintenance).
-- Run after 001-006 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (rooms are then open all day, beds always in use, no maintenance plans).

BEGIN;

-- Hours ELECTIVE cases may use a room (URGENT / EMERGENCY may use it any time). NULL = no limit.
ALTER TABLE public.operating_rooms ADD COLUMN IF NOT EXISTS elective_start TIME;
ALTER TABLE public.operating_rooms ADD COLUMN IF NOT EXISTS elective_end TIME;
DO $$
BEGIN
    ALTER TABLE public.operating_rooms ADD CONSTRAINT operating_rooms_elective_hours_valid
        CHECK (elective_start IS NULL OR elective_end IS NULL OR elective_end > elective_start) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Recovery beds: CLOSED beds are not used; available_from = occupied (e.g. by a ward patient) until.
ALTER TABLE public.recovery_beds ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'AVAILABLE';
ALTER TABLE public.recovery_beds ADD COLUMN IF NOT EXISTS note VARCHAR(200);
DO $$
BEGIN
    ALTER TABLE public.recovery_beds ADD CONSTRAINT recovery_beds_status_valid
        CHECK (status IN ('AVAILABLE', 'CLOSED')) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Expected stay in PACU / ICU after the procedure (minutes). NULL = PACU 60 min, ICU 24 h.
ALTER TABLE public.procedures ADD COLUMN IF NOT EXISTS recovery_mins INT;
DO $$
BEGIN
    ALTER TABLE public.procedures ADD CONSTRAINT procedures_recovery_valid
        CHECK (recovery_mins IS NULL OR recovery_mins BETWEEN 0 AND 10080) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Planned maintenance of a machine or set (service visit, repair): not used then.
CREATE TABLE IF NOT EXISTS public.equipment_maintenance (
    maintenance_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    unit_id        UUID NOT NULL REFERENCES public.equipment_units(unit_id) ON DELETE CASCADE,
    start_time     TIMESTAMPTZ NOT NULL,
    end_time       TIMESTAMPTZ NOT NULL,
    reason         VARCHAR(200),
    created_by     VARCHAR(200),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT equipment_maintenance_time_order CHECK (end_time > start_time)
);
CREATE INDEX IF NOT EXISTS equipment_maintenance_unit_time_idx ON public.equipment_maintenance (unit_id, start_time);
ALTER TABLE public.equipment_maintenance ENABLE ROW LEVEL SECURITY;  -- no policies: only the app server reads it

COMMIT;
