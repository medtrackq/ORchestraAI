-- ORchestraAI migration 006: single-use supplies and implants (Equipment & Inventory Agent).
-- Run after 001-005 (Supabase: SQL Editor -> paste -> Run). Safe to run again.
-- The app also works before this runs (supplies are then not checked).

BEGIN;

-- Stocked items: implants (knee set, mesh, graft) and consumables (stapler reload, drape kit).
CREATE TABLE IF NOT EXISTS public.supply_items (
    item_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code          VARCHAR(50) UNIQUE NOT NULL,
    name          VARCHAR(200) NOT NULL,
    category      VARCHAR(20) NOT NULL DEFAULT 'CONSUMABLE',
    unit          VARCHAR(30) NOT NULL DEFAULT 'ชิ้น',
    on_hand       INT NOT NULL DEFAULT 0,      -- counted in the store room
    reorder_level INT NOT NULL DEFAULT 0,      -- warn when what is free drops to this
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    CONSTRAINT supply_items_category_valid CHECK (category IN ('IMPLANT', 'CONSUMABLE')),
    CONSTRAINT supply_items_counts_valid CHECK (on_hand >= 0 AND reorder_level >= 0)
);

-- Orders on their way; counted for cases on or after the expected date.
CREATE TABLE IF NOT EXISTS public.supply_deliveries (
    delivery_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    item_id       UUID NOT NULL REFERENCES public.supply_items(item_id) ON DELETE CASCADE,
    quantity      INT NOT NULL CHECK (quantity > 0),
    expected_date DATE NOT NULL,
    status        VARCHAR(20) NOT NULL DEFAULT 'EXPECTED',  -- EXPECTED / RECEIVED / CANCELLED
    note          VARCHAR(200),
    created_by    VARCHAR(200),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    received_at   TIMESTAMPTZ,
    CONSTRAINT supply_deliveries_status_valid CHECK (status IN ('EXPECTED', 'RECEIVED', 'CANCELLED'))
);
CREATE INDEX IF NOT EXISTS supply_deliveries_item_idx ON public.supply_deliveries (item_id, expected_date)
    WHERE status = 'EXPECTED';

-- What a procedure normally uses (pre-filled in the booking form, editable per case).
CREATE TABLE IF NOT EXISTS public.procedure_supplies (
    procedure_id UUID NOT NULL REFERENCES public.procedures(procedure_id) ON DELETE CASCADE,
    item_id      UUID NOT NULL REFERENCES public.supply_items(item_id) ON DELETE CASCADE,
    quantity     INT NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (procedure_id, item_id)
);

-- What each booked request holds. RESERVED until the case has ended, then USED
-- (and taken off supply_items.on_hand). Rows follow the request, not the
-- schedule row, so moving a case keeps its reservation.
CREATE TABLE IF NOT EXISTS public.supply_reservations (
    request_id UUID NOT NULL REFERENCES public.surgery_requests(request_id) ON DELETE CASCADE,
    item_id    UUID NOT NULL REFERENCES public.supply_items(item_id) ON DELETE CASCADE,
    quantity   INT NOT NULL CHECK (quantity > 0),
    status     VARCHAR(20) NOT NULL DEFAULT 'RESERVED',
    used_at    TIMESTAMPTZ,
    PRIMARY KEY (request_id, item_id),
    CONSTRAINT supply_reservations_status_valid CHECK (status IN ('RESERVED', 'USED'))
);
CREATE INDEX IF NOT EXISTS supply_reservations_item_idx ON public.supply_reservations (item_id) WHERE status = 'RESERVED';

-- No policies: only the app server (database owner connection) reads these tables.
ALTER TABLE public.supply_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.supply_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.procedure_supplies ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.supply_reservations ENABLE ROW LEVEL SECURITY;

COMMIT;
