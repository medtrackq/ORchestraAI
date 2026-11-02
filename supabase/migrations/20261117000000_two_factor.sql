-- ORchestraAI migration 018: two-step sign-in (a 6-digit code from an authenticator app).
-- Run after 001-017 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (sign-in with a password only, as before).

BEGIN;

-- The authenticator secret of each account (base32, as the app on the phone holds it), when two-step
-- sign-in was switched on, the last 30-second step whose code was used (a code works once), and the
-- one-time recovery codes (SHA-256 hashes only; shown once when made).
ALTER TABLE public.app_users ADD COLUMN IF NOT EXISTS totp_secret     VARCHAR(64);
ALTER TABLE public.app_users ADD COLUMN IF NOT EXISTS totp_enabled_at TIMESTAMPTZ;
ALTER TABLE public.app_users ADD COLUMN IF NOT EXISTS totp_last_step  BIGINT;
ALTER TABLE public.app_users ADD COLUMN IF NOT EXISTS recovery_codes  JSONB;

COMMIT;
