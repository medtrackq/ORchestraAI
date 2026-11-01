-- ORchestraAI migration 015: waiting list priority.
-- Run after 001-014 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (the waiting list is then ordered by urgency only, without due dates).

BEGIN;

-- The date a waiting case should be operated by (e.g. a cancer case within 4 weeks): the waiting list
-- is ordered by it and warns when it is near or past.
ALTER TABLE public.surgery_requests ADD COLUMN IF NOT EXISTS due_date DATE;
-- When the request (last) went onto the waiting list: added without a day, or taken off a day.
ALTER TABLE public.surgery_requests ADD COLUMN IF NOT EXISTS waitlisted_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS surgery_requests_waiting_idx ON public.surgery_requests (due_date)
    WHERE status = 'PENDING';

COMMIT;
