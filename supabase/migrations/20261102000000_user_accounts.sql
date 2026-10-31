-- ORchestraAI migration 003: user accounts with roles.
-- Run after 002 (Supabase: SQL Editor -> paste -> Run). Safe to run again.

BEGIN;

-- Accounts for the ORchestraAI web app. Passwords are stored as scrypt
-- hashes, never in plain text. RLS is on with no policies: only the app
-- server (database owner connection) can read this table.
CREATE TABLE IF NOT EXISTS public.app_users (
    user_id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    username             VARCHAR(80) UNIQUE NOT NULL,      -- stored in lower case
    display_name         VARCHAR(150) NOT NULL,
    role                 VARCHAR(20) NOT NULL,
    staff_id             UUID REFERENCES public.staff(staff_id) ON DELETE SET NULL,
    password_hash        TEXT NOT NULL,
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    must_change_password BOOLEAN NOT NULL DEFAULT TRUE,     -- temporary password set by an admin
    session_version      INT NOT NULL DEFAULT 1,            -- bumped to sign the user out everywhere
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at        TIMESTAMPTZ,
    CONSTRAINT app_users_role CHECK (role IN ('admin', 'scheduler', 'surgeon', 'cssd', 'staff')),
    CONSTRAINT app_users_username_lower CHECK (username = lower(username))
);
ALTER TABLE public.app_users ENABLE ROW LEVEL SECURITY;

-- Audit: who triggered each scheduler change. The activity table comes from
-- migration 002; if 002 has not been run yet, 002 creates it with this column.
DO $$
BEGIN
  IF to_regclass('public.orchestra_activity') IS NOT NULL THEN
    ALTER TABLE public.orchestra_activity ADD COLUMN IF NOT EXISTS actor VARCHAR(150);
  END IF;
END $$;

COMMIT;
