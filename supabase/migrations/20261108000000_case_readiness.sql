-- ORchestraAI migration 009: pre-operative readiness checklist per case.
-- Run after 001-008 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (the checklist is then read-only and empty).

BEGIN;

-- One row per checked item of a surgery request; an item without a row is "not done yet".
-- item: consent / anesthesia / labs / npo / blood / site (see app/readiness.py)
CREATE TABLE IF NOT EXISTS public.case_readiness (
    request_id UUID NOT NULL REFERENCES public.surgery_requests(request_id) ON DELETE CASCADE,
    item       VARCHAR(20) NOT NULL,
    status     VARCHAR(10) NOT NULL,         -- 'DONE' / 'NA' (not needed) / 'PROBLEM'
    note       VARCHAR(200),
    updated_by VARCHAR(200),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (request_id, item),
    CONSTRAINT case_readiness_status_valid CHECK (status IN ('DONE', 'NA', 'PROBLEM'))
);

-- Procedures that need blood reserved before the operation (the checklist asks for it).
ALTER TABLE public.procedures ADD COLUMN IF NOT EXISTS requires_blood BOOLEAN NOT NULL DEFAULT FALSE;

-- No policies: only the app server (database owner connection) reads this table.
ALTER TABLE public.case_readiness ENABLE ROW LEVEL SECURITY;

COMMIT;
