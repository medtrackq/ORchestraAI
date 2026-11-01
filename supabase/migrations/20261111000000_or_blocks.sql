-- ORchestraAI migration 012: weekly OR block time (e.g. OR-1 Monday 08:00-16:00 for orthopaedics).
-- Run after 001-011 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (no blocks: every elective case may use any room).

BEGIN;

-- A room's time on one weekday that belongs to a specialty (matched with staff.specialty) or to one surgeon.
-- Until release_days before the date it is theirs: other surgeons' elective cases cannot be booked into it.
CREATE TABLE IF NOT EXISTS public.or_blocks (
    block_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    or_id        UUID NOT NULL REFERENCES public.operating_rooms(or_id) ON DELETE CASCADE,
    weekday      SMALLINT NOT NULL,           -- 0 = Monday ... 6 = Sunday
    start_time   TIME NOT NULL,
    end_time     TIME NOT NULL,
    owner_kind   VARCHAR(10) NOT NULL,        -- 'SPECIALTY' / 'SURGEON'
    specialty    VARCHAR(100),
    surgeon_id   UUID REFERENCES public.staff(staff_id) ON DELETE CASCADE,
    release_days SMALLINT NOT NULL DEFAULT 2, -- open to everyone this many days before the date
    valid_from   DATE,
    valid_to     DATE,
    note         VARCHAR(200),
    created_by   VARCHAR(200),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT or_blocks_weekday_valid CHECK (weekday BETWEEN 0 AND 6),
    CONSTRAINT or_blocks_time_order CHECK (end_time > start_time),
    CONSTRAINT or_blocks_owner_valid CHECK (
        (owner_kind = 'SPECIALTY' AND specialty IS NOT NULL) OR (owner_kind = 'SURGEON' AND surgeon_id IS NOT NULL)),
    CONSTRAINT or_blocks_release_valid CHECK (release_days BETWEEN 0 AND 60)
);
CREATE INDEX IF NOT EXISTS or_blocks_room_day_idx ON public.or_blocks (or_id, weekday);

-- An owner gives one date of a block back early ("we will not use next Monday").
CREATE TABLE IF NOT EXISTS public.or_block_releases (
    block_id    UUID NOT NULL REFERENCES public.or_blocks(block_id) ON DELETE CASCADE,
    day         DATE NOT NULL,
    released_by VARCHAR(200),
    released_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (block_id, day)
);

-- No policies: only the app server (database owner connection) reads these tables.
ALTER TABLE public.or_blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.or_block_releases ENABLE ROW LEVEL SECURITY;

COMMIT;
