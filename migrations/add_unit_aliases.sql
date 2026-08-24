-- Price unit aliases for normalization (UAE / AED)
-- M = mn = million = millions
-- K = thousand = thousands = 000
-- abs = AED = Dhs = Dirham = Dirhams = absolute dirham amount

CREATE TABLE IF NOT EXISTS price_unit_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL UNIQUE,
    canonical_unit TEXT NOT NULL,  -- 'M', 'K', 'abs'
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_price_unit_aliases_alias ON price_unit_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_price_unit_aliases_canonical ON price_unit_aliases(canonical_unit);

INSERT OR IGNORE INTO price_unit_aliases (alias, canonical_unit) VALUES
    ('m', 'M'), ('mn', 'M'), ('million', 'M'), ('millions', 'M'),
    ('k', 'K'), ('thousand', 'K'), ('thousands', 'K'),
    ('aed', 'abs'), ('dhs', 'abs'), ('dirham', 'abs'), ('dirhams', 'abs'),
    ('abs', 'abs'), ('absolute', 'abs');
