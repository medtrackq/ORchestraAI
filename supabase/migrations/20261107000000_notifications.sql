-- ORchestraAI migration 008: notifications on phones (browser push and LINE).
-- Run after 001-007 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (phone notifications are then off).

BEGIN;

-- Where each user receives notifications: a browser push subscription or a linked LINE account.
-- user_id is text so the owner account from Render (not in app_users) can subscribe too.
CREATE TABLE IF NOT EXISTS public.notify_targets (
    channel    VARCHAR(10) NOT NULL,          -- 'webpush' / 'line'
    address    TEXT NOT NULL,                  -- push endpoint URL / LINE user id
    user_id    VARCHAR(64) NOT NULL,
    keys       JSONB NOT NULL DEFAULT '{}',    -- web push p256dh / auth
    label      VARCHAR(200),                   -- e.g. the browser
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (channel, address),
    CONSTRAINT notify_targets_channel_valid CHECK (channel IN ('webpush', 'line'))
);
CREATE INDEX IF NOT EXISTS notify_targets_user_idx ON public.notify_targets (user_id);

-- Which events each user wants: {"case": true, "emergency": false, "cssd": true}.
CREATE TABLE IF NOT EXISTS public.notify_prefs (
    user_id VARCHAR(64) PRIMARY KEY,
    prefs   JSONB NOT NULL DEFAULT '{}'
);

-- One-time codes a user sends to the hospital's LINE Official Account to link it.
CREATE TABLE IF NOT EXISTS public.line_link_codes (
    code       VARCHAR(12) PRIMARY KEY,
    user_id    VARCHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);

-- What was sent, to whom, and whether it arrived (no patient identifiers are ever sent).
CREATE TABLE IF NOT EXISTS public.notification_deliveries (
    delivery_id BIGSERIAL PRIMARY KEY,
    user_id     VARCHAR(64) NOT NULL,
    channel     VARCHAR(10) NOT NULL,
    kind        VARCHAR(20) NOT NULL,
    title       VARCHAR(200) NOT NULL,
    body        TEXT NOT NULL,
    status      VARCHAR(10) NOT NULL,          -- 'sent' / 'failed' / 'removed'
    error       VARCHAR(300),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS notification_deliveries_user_idx ON public.notification_deliveries (user_id, created_at DESC);

-- App-wide settings kept by the server (the browser-push VAPID key pair).
CREATE TABLE IF NOT EXISTS public.app_settings (
    key   VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL
);

-- No policies: only the app server (database owner connection) reads these tables.
ALTER TABLE public.notify_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notify_prefs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.line_link_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.notification_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.app_settings ENABLE ROW LEVEL SECURITY;

COMMIT;
