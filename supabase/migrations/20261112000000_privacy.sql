-- ORchestraAI migration 013: who looked at patient data and when (PDPA), and removing patient identity after a set time.
-- Run after 001-012 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (access is then not recorded and identity is never removed).

BEGIN;

-- One row each time a person was shown patients' HN / names: the day's schedule, a printout, a lookup.
-- Holds case codes only (not HN or names), so the log itself does not repeat the identities.
CREATE TABLE IF NOT EXISTS public.patient_access_log (
    access_id   BIGSERIAL PRIMARY KEY,
    accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id     VARCHAR(64) NOT NULL,
    username    VARCHAR(100) NOT NULL,
    role        VARCHAR(20) NOT NULL,
    action      VARCHAR(20) NOT NULL,        -- 'schedule' / 'print' / 'lookup'
    target_day  DATE,
    case_codes  TEXT[] NOT NULL DEFAULT '{}',
    patients    INTEGER NOT NULL DEFAULT 0,  -- how many patients' identities were shown
    address     VARCHAR(64)                  -- IP address of the request
);
CREATE INDEX IF NOT EXISTS patient_access_log_time_idx ON public.patient_access_log (accessed_at DESC);
CREATE INDEX IF NOT EXISTS patient_access_log_user_idx ON public.patient_access_log (username, accessed_at DESC);
CREATE INDEX IF NOT EXISTS patient_access_log_cases_idx ON public.patient_access_log USING GIN (case_codes);
ALTER TABLE public.patient_access_log ENABLE ROW LEVEL SECURITY;  -- no policies: only the app server reads it

-- When a finished case's HN, name and free text were removed (retention period over).
ALTER TABLE public.surgery_requests ADD COLUMN IF NOT EXISTS anonymized_at TIMESTAMPTZ;

COMMIT;
