-- 0004_symbol_secid.sql — remember the resolved Eastmoney security id

ALTER TABLE symbol ADD COLUMN secid TEXT;
ALTER TABLE symbol ADD COLUMN resolve_error TEXT;

CREATE INDEX IF NOT EXISTS idx_symbol_secid ON symbol(secid);
