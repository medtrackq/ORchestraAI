-- ORchestraAI migration 004: Staffing Agent fatigue limits and required skills per procedure.
-- Run after 001-003 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (it then uses 10 h/day and 8 h rest for everyone).

BEGIN;

-- Per-person limits (the hospital can set them per person; defaults below).
ALTER TABLE public.staff ADD COLUMN IF NOT EXISTS max_hours_per_day NUMERIC(4,1) NOT NULL DEFAULT 10;
ALTER TABLE public.staff ADD COLUMN IF NOT EXISTS min_rest_hours NUMERIC(4,1) NOT NULL DEFAULT 8;
DO $$
BEGIN
    ALTER TABLE public.staff ADD CONSTRAINT staff_fatigue_limits_valid CHECK (
        max_hours_per_week > 0 AND max_hours_per_day > 0 AND max_hours_per_day <= 24
        AND min_rest_hours >= 0 AND min_rest_hours <= 24
    ) NOT VALID;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMENT ON COLUMN public.staff.max_hours_per_week IS
    'Limit of operating-room hours in any 7 days (Staffing Agent).';
COMMENT ON COLUMN public.staff.max_hours_per_day IS
    'Limit of operating-room hours in one day (Staffing Agent).';
COMMENT ON COLUMN public.staff.min_rest_hours IS
    'Rest before the next case after a case that ends after 22:00 or by 06:00 (Staffing Agent).';
COMMENT ON COLUMN public.staff.current_weekly_hours IS
    'No longer read by ORchestraAI: hours worked are computed from scheduled_surgeries (actual times when recorded).';

-- Skills each team role must have for a procedure, e.g. {"SCRUB_NURSE": ["ROBOTIC_CERTIFIED"]}.
ALTER TABLE public.procedures ADD COLUMN IF NOT EXISTS required_staff_skills JSONB NOT NULL DEFAULT '{}'::jsonb;
COMMENT ON COLUMN public.procedures.required_staff_skills IS
    'Role -> skills every person in that role must have (matched against staff.skills).';

-- Robotic procedures need a robotic-certified scrub nurse (the old "Robotic nurse" checkbox).
UPDATE public.procedures
SET required_staff_skills = '{"SCRUB_NURSE": ["ROBOTIC_CERTIFIED"]}'::jsonb
WHERE required_room_capabilities ? 'ROBOTIC_READY'
  AND required_staff_skills = '{}'::jsonb;

-- 7-day look-back of each person's cases.
CREATE INDEX IF NOT EXISTS resource_allocations_staff_time_idx
    ON public.resource_allocations (staff_id, allocated_from) WHERE staff_id IS NOT NULL;

COMMIT;
