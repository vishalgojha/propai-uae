-- Rent benchmarks for Dubai localities, computed by scripts/dld_import.py
-- from the DLD Ejari rent-contract snapshot (dld_rent_contracts-open).
-- One jsonb document per locality keeps consumers on a single read while
-- the importer owns its shape and refresh cadence.
alter table public.locality_reference add column if not exists rent_benchmarks jsonb;