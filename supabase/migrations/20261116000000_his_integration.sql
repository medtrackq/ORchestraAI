-- ORchestraAI migration 017: connection to the hospital information system (HIS).
-- Run after 001-016 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (no HIS connection; requests are typed in the app as before).

BEGIN;

-- Keys the HIS uses to call the ORchestraAI API (only a hash is kept; the key is shown once).
CREATE TABLE IF NOT EXISTS public.his_api_keys (
    key_id       UUID PRIMARY KEY,
    name         VARCHAR(100) NOT NULL,
    prefix       VARCHAR(16) NOT NULL,
    key_hash     VARCHAR(64) NOT NULL UNIQUE,
    scopes       TEXT[] NOT NULL DEFAULT '{}',
    created_by   VARCHAR(200),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);

-- HIS codes (doctor, procedure) matched to ORchestraAI staff and procedures.
CREATE TABLE IF NOT EXISTS public.his_mappings (
    kind        VARCHAR(20) NOT NULL,
    his_code    VARCHAR(100) NOT NULL,
    target_id   VARCHAR(100) NOT NULL,
    his_label   VARCHAR(200),
    updated_by  VARCHAR(200),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (kind, his_code),
    CONSTRAINT his_mappings_kind_valid CHECK (kind IN ('surgeon', 'procedure'))
);

-- Every message received from the HIS, with what was done with it. The message itself (it holds the
-- HN and name) is cleared once applied, and after 30 days if it never could be.
CREATE TABLE IF NOT EXISTS public.his_inbox (
    message_id   BIGSERIAL PRIMARY KEY,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source       VARCHAR(20) NOT NULL DEFAULT 'api',
    key_id       UUID,
    kind         VARCHAR(30) NOT NULL,
    his_ref      VARCHAR(100),
    status       VARCHAR(20) NOT NULL,
    detail       VARCHAR(500),
    missing      JSONB,
    request_id   UUID,
    payload      JSONB,
    CONSTRAINT his_inbox_status_valid CHECK (status IN ('APPLIED', 'UNCHANGED', 'NEEDS_MAPPING', 'NEEDS_ACTION', 'REJECTED'))
);
CREATE INDEX IF NOT EXISTS his_inbox_recent_idx ON public.his_inbox (received_at DESC);
CREATE INDEX IF NOT EXISTS his_inbox_ref_idx ON public.his_inbox (his_ref);

-- Requests that came from the HIS keep its order number (sent again = the same request).
ALTER TABLE public.surgery_requests ADD COLUMN IF NOT EXISTS his_ref VARCHAR(100);
ALTER TABLE public.surgery_requests ADD COLUMN IF NOT EXISTS requested_date DATE;
CREATE UNIQUE INDEX IF NOT EXISTS surgery_requests_his_ref_key ON public.surgery_requests (his_ref) WHERE his_ref IS NOT NULL;

ALTER TABLE public.his_api_keys ENABLE ROW LEVEL SECURITY;  -- no policies: only the app server reads them
ALTER TABLE public.his_mappings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.his_inbox ENABLE ROW LEVEL SECURITY;

COMMIT;
