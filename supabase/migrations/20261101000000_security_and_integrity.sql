-- ORchestraAI migration 002: security and booking-integrity fixes.
-- Run once after 20261031000000_initial_schema.sql (Supabase: SQL Editor -> paste -> Run).
-- Existing rows are kept. Safe to run again.

BEGIN;

-- 1) Row Level Security on every table.
--    No policies are added, so the public Supabase API (anon / authenticated
--    keys) can no longer read or change these tables, including patient names.
--    The ORchestraAI server connects as the database owner and is unaffected.
ALTER TABLE public.staff                    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.staff_schedules          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.team_synergy             ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.equipment_types          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.equipment_units          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.operating_rooms          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.recovery_beds            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.procedures               ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.surgeon_preference_cards ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.surgery_requests         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scheduled_surgeries      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.resource_allocations     ENABLE ROW LEVEL SECURITY;

-- 2) Cancelled bookings no longer block the room, and a recovery bed cannot
--    be reserved twice for overlapping times.
ALTER TABLE public.scheduled_surgeries ADD COLUMN IF NOT EXISTS recovery_end_time TIMESTAMPTZ;

DO $$
DECLARE c record;
BEGIN
  FOR c IN
    SELECT conname FROM pg_constraint
    WHERE conrelid = 'public.scheduled_surgeries'::regclass AND contype = 'x'
  LOOP
    EXECUTE format('ALTER TABLE public.scheduled_surgeries DROP CONSTRAINT %I', c.conname);
  END LOOP;
END $$;

ALTER TABLE public.scheduled_surgeries
  ADD CONSTRAINT scheduled_surgeries_room_no_overlap
  EXCLUDE USING gist (or_id WITH =, tstzrange(start_time, cleanup_end_time) WITH &&)
  WHERE (status IS DISTINCT FROM 'CANCELLED');

ALTER TABLE public.scheduled_surgeries
  ADD CONSTRAINT scheduled_surgeries_bed_no_overlap
  EXCLUDE USING gist (reserved_bed_id WITH =, tstzrange(end_time, recovery_end_time) WITH &&)
  WHERE (reserved_bed_id IS NOT NULL AND recovery_end_time IS NOT NULL AND status IS DISTINCT FROM 'CANCELLED');

-- 3) Basic sanity checks. NOT VALID: applied to new and changed rows only,
--    so older rows that break them do not stop this migration.
ALTER TABLE public.scheduled_surgeries DROP CONSTRAINT IF EXISTS scheduled_surgeries_time_order;
ALTER TABLE public.scheduled_surgeries
  ADD CONSTRAINT scheduled_surgeries_time_order
  CHECK (start_time < end_time AND end_time <= cleanup_end_time) NOT VALID;

ALTER TABLE public.resource_allocations DROP CONSTRAINT IF EXISTS resource_allocations_one_resource;
ALTER TABLE public.resource_allocations
  ADD CONSTRAINT resource_allocations_one_resource
  CHECK ((resource_type = 'STAFF' AND staff_id IS NOT NULL AND equipment_unit_id IS NULL)
      OR (resource_type = 'EQUIPMENT' AND equipment_unit_id IS NOT NULL AND staff_id IS NULL)) NOT VALID;

ALTER TABLE public.resource_allocations DROP CONSTRAINT IF EXISTS resource_allocations_time_order;
ALTER TABLE public.resource_allocations
  ADD CONSTRAINT resource_allocations_time_order CHECK (allocated_from < allocated_until) NOT VALID;

ALTER TABLE public.surgery_requests DROP CONSTRAINT IF EXISTS surgery_requests_urgency_level;
ALTER TABLE public.surgery_requests
  ADD CONSTRAINT surgery_requests_urgency_level
  CHECK (urgency_level IN ('ELECTIVE', 'URGENT', 'EMERGENCY')) NOT VALID;

-- 4) Lookups the scheduler runs on every request.
CREATE INDEX IF NOT EXISTS scheduled_surgeries_start_idx ON public.scheduled_surgeries (start_time);
CREATE INDEX IF NOT EXISTS staff_schedules_staff_time_idx ON public.staff_schedules (staff_id, shift_start);
CREATE INDEX IF NOT EXISTS resource_allocations_schedule_idx ON public.resource_allocations (schedule_id);

-- 5) Activity log: what the scheduler changed and who was notified.
CREATE TABLE IF NOT EXISTS public.orchestra_activity (
    activity_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    planning_date DATE NOT NULL,
    kind          VARCHAR(30) NOT NULL,   -- 'booking', 'emergency', 'overrun', 'seed'
    title         TEXT NOT NULL,
    agent_log     JSONB NOT NULL DEFAULT '[]',
    changes       JSONB NOT NULL DEFAULT '[]',
    notifications JSONB NOT NULL DEFAULT '[]',
    actor         VARCHAR(150)            -- who triggered the change (see migration 003)
);
CREATE INDEX IF NOT EXISTS orchestra_activity_date_idx ON public.orchestra_activity (planning_date, created_at DESC);
ALTER TABLE public.orchestra_activity ENABLE ROW LEVEL SECURITY;

COMMIT;
