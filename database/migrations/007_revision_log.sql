-- مایگریشن 007: دفتر اصلاحات داده (Revision Log)
-- وقتی منبع (TSETMC) عددی از گذشته را تغییر می‌دهد، مقدار قبلی دور ریخته نمی‌شود و در این جدول‌ها می‌ماند.
-- فقط «تغییر واقعی» ثبت می‌شود (به‌روزرسانی با همان اعداد ردیفی نمی‌سازد) و از داده موجود کپی اولیه گرفته
-- نمی‌شود؛ پس حجم دیتابیس تقریباً ثابت می‌ماند. مثال رایج: کندل موقت عصر (از دیده‌بان) که روز بعد با
-- داده رسمی تاریخچه جایگزین می‌شود.
-- revised_at = زمان دریافت مقدار جدید (captured_at ردیف جدید)، previous_captured_at = زمان دریافت مقدار قبلی.

CREATE TABLE IF NOT EXISTS daily_prices_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    daily_price_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    source TEXT,
    open_raw REAL,
    high_raw REAL,
    low_raw REAL,
    close_raw REAL,
    close_final_raw REAL,
    ref_price_raw REAL,
    volume INTEGER,
    value REAL,
    trade_count INTEGER,
    previous_captured_at TEXT,
    revised_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_daily_prices_revisions_symbol_date ON daily_prices_revisions(symbol_id, date);

CREATE TRIGGER IF NOT EXISTS trg_daily_prices_revision
AFTER UPDATE OF open_raw, high_raw, low_raw, close_raw, close_final_raw, ref_price_raw, volume, value, trade_count
ON daily_prices
WHEN OLD.open_raw IS NOT NEW.open_raw OR OLD.high_raw IS NOT NEW.high_raw OR OLD.low_raw IS NOT NEW.low_raw
  OR OLD.close_raw IS NOT NEW.close_raw OR OLD.close_final_raw IS NOT NEW.close_final_raw
  OR OLD.ref_price_raw IS NOT NEW.ref_price_raw OR OLD.volume IS NOT NEW.volume OR OLD.value IS NOT NEW.value
  OR OLD.trade_count IS NOT NEW.trade_count
BEGIN
    INSERT INTO daily_prices_revisions (
        daily_price_id, symbol_id, date, source, open_raw, high_raw, low_raw, close_raw, close_final_raw,
        ref_price_raw, volume, value, trade_count, previous_captured_at, revised_at
    ) VALUES (
        OLD.id, OLD.symbol_id, OLD.date, OLD.source, OLD.open_raw, OLD.high_raw, OLD.low_raw, OLD.close_raw,
        OLD.close_final_raw, OLD.ref_price_raw, OLD.volume, OLD.value, OLD.trade_count, OLD.captured_at,
        NEW.captured_at
    );
END;

CREATE TABLE IF NOT EXISTS client_types_daily_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_type_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    buy_i_volume REAL, buy_n_volume REAL, buy_i_value REAL, buy_n_value REAL,
    buy_i_count REAL, buy_n_count REAL,
    sell_i_volume REAL, sell_n_volume REAL, sell_i_value REAL, sell_n_value REAL,
    sell_i_count REAL, sell_n_count REAL,
    previous_captured_at TEXT,
    revised_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_client_types_revisions_symbol_date ON client_types_daily_revisions(symbol_id, date);

CREATE TRIGGER IF NOT EXISTS trg_client_types_revision
AFTER UPDATE ON client_types_daily
WHEN OLD.buy_i_volume IS NOT NEW.buy_i_volume OR OLD.buy_n_volume IS NOT NEW.buy_n_volume
  OR OLD.buy_i_value IS NOT NEW.buy_i_value OR OLD.buy_n_value IS NOT NEW.buy_n_value
  OR OLD.buy_i_count IS NOT NEW.buy_i_count OR OLD.buy_n_count IS NOT NEW.buy_n_count
  OR OLD.sell_i_volume IS NOT NEW.sell_i_volume OR OLD.sell_n_volume IS NOT NEW.sell_n_volume
  OR OLD.sell_i_value IS NOT NEW.sell_i_value OR OLD.sell_n_value IS NOT NEW.sell_n_value
  OR OLD.sell_i_count IS NOT NEW.sell_i_count OR OLD.sell_n_count IS NOT NEW.sell_n_count
BEGIN
    INSERT INTO client_types_daily_revisions (
        client_type_id, symbol_id, date, buy_i_volume, buy_n_volume, buy_i_value, buy_n_value, buy_i_count,
        buy_n_count, sell_i_volume, sell_n_volume, sell_i_value, sell_n_value, sell_i_count, sell_n_count,
        previous_captured_at, revised_at
    ) VALUES (
        OLD.id, OLD.symbol_id, OLD.date, OLD.buy_i_volume, OLD.buy_n_volume, OLD.buy_i_value, OLD.buy_n_value,
        OLD.buy_i_count, OLD.buy_n_count, OLD.sell_i_volume, OLD.sell_n_volume, OLD.sell_i_value,
        OLD.sell_n_value, OLD.sell_i_count, OLD.sell_n_count, OLD.captured_at, NEW.captured_at
    );
END;
