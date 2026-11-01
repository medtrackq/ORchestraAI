-- ORchestraAI migration 011: the time of each step of a case on the day (status board, reports).
-- Run after 001-010 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs: into / out of the OR are kept in
-- scheduled_surgeries.actual_start_time / actual_end_time as before; the other steps need this table.

BEGIN;

-- step: arrived (pre-op holding) / incision / closure / recovery_out (left recovery); see app/milestones.py
CREATE TABLE IF NOT EXISTS public.case_milestones (
    request_id  UUID NOT NULL REFERENCES public.surgery_requests(request_id) ON DELETE CASCADE,
    step        VARCHAR(20) NOT NULL,
    at          TIMESTAMPTZ NOT NULL,
    recorded_by VARCHAR(200),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (request_id, step)
);

-- No policies: only the app server (database owner connection) reads this table.
ALTER TABLE public.case_milestones ENABLE ROW LEVEL SECURITY;

COMMIT;
