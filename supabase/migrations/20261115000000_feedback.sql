-- ORchestraAI migration 016: problem reports and suggestions from users, and errors the system ran into.
-- Run after 001-015 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (reports and errors are then kept in memory until the server restarts).

BEGIN;

-- Sent with the button on every page. The text is scrubbed of HN / ID / phone numbers and names after a
-- title before it is stored; the page and the time are attached, patient data is not.
CREATE TABLE IF NOT EXISTS public.app_feedback (
    feedback_id   BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind          VARCHAR(10) NOT NULL DEFAULT 'PROBLEM',
    message       VARCHAR(2000) NOT NULL,
    page          VARCHAR(200),
    user_agent    VARCHAR(200),
    user_id       VARCHAR(64) NOT NULL,
    username      VARCHAR(100),
    display_name  VARCHAR(200),
    role          VARCHAR(20),
    status        VARCHAR(10) NOT NULL DEFAULT 'NEW',
    admin_note    VARCHAR(500),
    handled_by    VARCHAR(200),
    updated_at    TIMESTAMPTZ,
    CONSTRAINT app_feedback_kind_valid CHECK (kind IN ('PROBLEM', 'IDEA', 'QUESTION')),
    CONSTRAINT app_feedback_status_valid CHECK (status IN ('NEW', 'SEEN', 'DONE'))
);
CREATE INDEX IF NOT EXISTS app_feedback_open_idx ON public.app_feedback (created_at DESC) WHERE status <> 'DONE';
CREATE INDEX IF NOT EXISTS app_feedback_user_idx ON public.app_feedback (user_id, created_at DESC);

-- One row per kind of error (same place, same message), counted.
CREATE TABLE IF NOT EXISTS public.app_errors (
    error_id     BIGSERIAL PRIMARY KEY,
    fingerprint  VARCHAR(64) NOT NULL UNIQUE,
    source       VARCHAR(10) NOT NULL,
    where_at     VARCHAR(200),
    message      VARCHAR(500),
    page         VARCHAR(200),
    last_user    VARCHAR(100),
    occurrences  INTEGER NOT NULL DEFAULT 1,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status       VARCHAR(10) NOT NULL DEFAULT 'NEW',
    reopened     BOOLEAN NOT NULL DEFAULT false,   -- came back after it was marked fixed
    admin_note   VARCHAR(500),
    CONSTRAINT app_errors_status_valid CHECK (status IN ('NEW', 'DONE'))
);
CREATE INDEX IF NOT EXISTS app_errors_open_idx ON public.app_errors (last_seen DESC) WHERE status <> 'DONE';

ALTER TABLE public.app_feedback ENABLE ROW LEVEL SECURITY;  -- no policies: only the app server reads them
ALTER TABLE public.app_errors ENABLE ROW LEVEL SECURITY;

COMMIT;
