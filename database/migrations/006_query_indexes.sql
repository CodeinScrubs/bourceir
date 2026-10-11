-- BRS Migration 006: Query indexes
-- پرس‌وجوهای مقطعی (همه نمادها در یک روز) برای نبض بازار و ساخت فیچرهای بازار
CREATE INDEX IF NOT EXISTS idx_daily_prices_date ON daily_prices(date);
CREATE INDEX IF NOT EXISTS idx_client_types_daily_date ON client_types_daily(date);
CREATE INDEX IF NOT EXISTS idx_announcements_symbol ON announcements(symbol_id, published_at);
CREATE INDEX IF NOT EXISTS idx_symbols_active_type ON symbols(is_active, instrument_type_code);
