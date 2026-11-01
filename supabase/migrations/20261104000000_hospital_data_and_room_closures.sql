-- ORchestraAI migration 005: hospital data pages, planned room closures, airborne-infection cases.
-- Run after 001-004 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (closures, airborne flag and leave notes are then unavailable).

BEGIN;

-- Planned closed periods of an operating room (repair, deep cleaning, inspection).
CREATE TABLE IF NOT EXISTS public.room_closures (
    closure_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    or_id      UUID NOT NULL REFERENCES public.operating_rooms(or_id) ON DELETE CASCADE,
    start_time TIMESTAMPTZ NOT NULL,
    end_time   TIMESTAMPTZ NOT NULL,
    reason     VARCHAR(200),
    created_by VARCHAR(200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT room_closures_time_order CHECK (end_time > start_time)
);
CREATE INDEX IF NOT EXISTS room_closures_room_time_idx ON public.room_closures (or_id, start_time);
ALTER TABLE public.room_closures ENABLE ROW LEVEL SECURITY;  -- no policies: only the app server reads it

-- Patient with an airborne infection (TB, measles, ...): needs a negative-pressure room.
ALTER TABLE public.surgery_requests ADD COLUMN IF NOT EXISTS airborne_isolation BOOLEAN NOT NULL DEFAULT FALSE;

-- Leave / shift note (e.g. "ลาพักร้อน", "ประชุมวิชาการ").
ALTER TABLE public.staff_schedules ADD COLUMN IF NOT EXISTS note VARCHAR(200);

DO $$
BEGIN
    ALTER TABLE public.staff_schedules ADD CONSTRAINT staff_schedules_time_order CHECK (shift_end > shift_start) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$
BEGIN
    ALTER TABLE public.staff_schedules ADD CONSTRAINT staff_schedules_type_valid
        CHECK (shift_type IN ('REGULAR', 'ON_CALL', 'LEAVE')) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
