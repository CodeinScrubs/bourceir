-- رسید فقط‌افزودنی بازیابی قیمت از ریزمعاملات؛ داده قبلی در دفتر اصلاحات 007 هم حفظ می‌شود.
CREATE TABLE IF NOT EXISTS price_recoveries (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    symbol_id INTEGER NOT NULL REFERENCES symbols(id),
    daily_price_id INTEGER NOT NULL REFERENCES daily_prices(id),
    date TEXT NOT NULL,
    applied_at TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(run_id, daily_price_id)
);
CREATE INDEX IF NOT EXISTS idx_price_recoveries_symbol_date ON price_recoveries(symbol_id, date);
CREATE TRIGGER IF NOT EXISTS price_recoveries_no_update
BEFORE UPDATE ON price_recoveries BEGIN
    SELECT RAISE(ABORT, 'price recovery receipts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS price_recoveries_no_delete
BEFORE DELETE ON price_recoveries BEGIN
    SELECT RAISE(ABORT, 'price recovery receipts are append-only');
END;
