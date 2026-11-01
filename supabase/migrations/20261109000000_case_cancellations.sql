-- ORchestraAI migration 010: why cases were cancelled or postponed (for the management report).
-- Run after 001-009 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (cases can still be cancelled; reasons are not kept).

BEGIN;

-- One row each time a case comes off its day (or a waiting request is given up), with the reason.
-- A case postponed twice has two rows. reason: see app/cancellations.py.
CREATE TABLE IF NOT EXISTS public.case_cancellations (
    cancellation_id BIGSERIAL PRIMARY KEY,
    request_id      UUID NOT NULL REFERENCES public.surgery_requests(request_id) ON DELETE CASCADE,
    case_code       VARCHAR(40),
    planned_start   TIMESTAMPTZ,            -- NULL: it was on the waiting list, not on a day
    room_code       VARCHAR(20),
    surgeon_id      UUID REFERENCES public.staff(staff_id) ON DELETE SET NULL,
    procedure_name  VARCHAR(200),
    reason          VARCHAR(30) NOT NULL,
    note            VARCHAR(200),
    outcome         VARCHAR(12) NOT NULL,   -- 'POSTPONED' (waits for a new day) / 'CANCELLED'
    cancelled_by    VARCHAR(200),
    cancelled_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT case_cancellations_outcome_valid CHECK (outcome IN ('POSTPONED', 'CANCELLED'))
);
CREATE INDEX IF NOT EXISTS case_cancellations_planned_idx ON public.case_cancellations (planned_start);
CREATE INDEX IF NOT EXISTS case_cancellations_at_idx ON public.case_cancellations (cancelled_at);

-- No policies: only the app server (database owner connection) reads this table.
ALTER TABLE public.case_cancellations ENABLE ROW LEVEL SECURITY;

COMMIT;
