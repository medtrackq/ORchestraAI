-- ORchestraAI migration 019: hospital master data from the HIS (HL7 FHIR R4): staff and their rosters,
-- operating rooms and equipment.
-- Run after 001-018 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (staff, rosters, rooms and equipment are kept in the app as before).

BEGIN;

-- A record (or a shift) that came from the HIS keeps the source here: the fields the HIS owns cannot be
-- changed in the app or by an Excel import, only by the HIS. NULL = the app owns the whole record.
ALTER TABLE public.operating_rooms ADD COLUMN IF NOT EXISTS managed_by VARCHAR(30);
ALTER TABLE public.staff ADD COLUMN IF NOT EXISTS managed_by VARCHAR(30);
ALTER TABLE public.equipment_units ADD COLUMN IF NOT EXISTS managed_by VARCHAR(30);
ALTER TABLE public.staff_schedules ADD COLUMN IF NOT EXISTS managed_by VARCHAR(30);

-- Which HIS resource is which record here (e.g. "Practitioner/123" -> a staff id): a roster or a device
-- that points at a resource sent earlier is matched through this.
CREATE TABLE IF NOT EXISTS public.his_external_refs (
    entity_type  VARCHAR(20) NOT NULL,
    external_id  VARCHAR(200) NOT NULL,
    local_id     VARCHAR(100) NOT NULL,
    label        VARCHAR(200),
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_type, external_id),
    CONSTRAINT his_external_refs_type_valid CHECK (entity_type IN ('room', 'staff', 'equipment', 'location'))
);
CREATE INDEX IF NOT EXISTS his_external_refs_local_idx ON public.his_external_refs (entity_type, local_id);

-- Every sync (HIS push, the app pulling from the HIS, an admin's file), trial runs too: counts and problems.
-- No patient data: staff names are in the records themselves, not here.
CREATE TABLE IF NOT EXISTS public.his_sync_runs (
    run_id       BIGSERIAL PRIMARY KEY,
    mode         VARCHAR(10) NOT NULL,
    dry_run      BOOLEAN NOT NULL DEFAULT FALSE,
    status       VARCHAR(10) NOT NULL,
    stats        JSONB NOT NULL DEFAULT '{}',
    problems     JSONB NOT NULL DEFAULT '[]',
    started_by   VARCHAR(200),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    CONSTRAINT his_sync_runs_mode_valid CHECK (mode IN ('push', 'pull', 'file')),
    CONSTRAINT his_sync_runs_status_valid CHECK (status IN ('success', 'partial', 'failed'))
);
CREATE INDEX IF NOT EXISTS his_sync_runs_recent_idx ON public.his_sync_runs (started_at DESC);

ALTER TABLE public.his_external_refs ENABLE ROW LEVEL SECURITY;  -- no policies: only the app server reads them
ALTER TABLE public.his_sync_runs ENABLE ROW LEVEL SECURITY;

COMMIT;
