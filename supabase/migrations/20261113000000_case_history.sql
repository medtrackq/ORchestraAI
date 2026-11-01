-- ORchestraAI migration 014: past surgeries imported from Excel, so the duration forecast learns from day one.
-- Run after 001-013 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (the forecast then learns only from cases recorded in the app).

BEGIN;

-- One uploaded file (an import can be removed as a whole if it was the wrong file).
CREATE TABLE IF NOT EXISTS public.case_history_imports (
    batch_id    UUID PRIMARY KEY,
    filename    VARCHAR(200),
    rows        INTEGER NOT NULL,
    imported_by VARCHAR(200),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- A past case: who did which procedure, when, and how long it took (in the room until out of the room).
-- No patient identity is kept: the forecast and the reports do not need it.
CREATE TABLE IF NOT EXISTS public.case_history (
    history_id   BIGSERIAL PRIMARY KEY,
    batch_id     UUID NOT NULL REFERENCES public.case_history_imports(batch_id) ON DELETE CASCADE,
    surgery_date DATE NOT NULL,
    start_time   TIME,
    surgeon_id   UUID NOT NULL REFERENCES public.staff(staff_id) ON DELETE CASCADE,
    procedure_id UUID NOT NULL REFERENCES public.procedures(procedure_id) ON DELETE CASCADE,
    room_code    VARCHAR(20),
    urgency      VARCHAR(10) NOT NULL DEFAULT 'ELECTIVE',
    booked_mins  INTEGER,
    actual_mins  INTEGER NOT NULL,
    CONSTRAINT case_history_minutes_valid CHECK (actual_mins BETWEEN 5 AND 1440),
    CONSTRAINT case_history_urgency_valid CHECK (urgency IN ('ELECTIVE', 'URGENT', 'EMERGENCY'))
);
-- The same case uploaded twice (or in two files) is kept once.
CREATE UNIQUE INDEX IF NOT EXISTS case_history_same_case ON public.case_history
    (surgery_date, surgeon_id, procedure_id, COALESCE(room_code, ''), COALESCE(start_time, '00:00'::time), actual_mins);
CREATE INDEX IF NOT EXISTS case_history_pair_idx ON public.case_history (surgeon_id, procedure_id);

ALTER TABLE public.case_history_imports ENABLE ROW LEVEL SECURITY;  -- no policies: only the app server reads them
ALTER TABLE public.case_history ENABLE ROW LEVEL SECURITY;

COMMIT;
