# ساختار جداول پایگاه داده BRS (Database Schema)

پایگاه داده SQLite در مسیر `data/brs.db` است (حالت WAL برای خواندن هم‌زمان داشبورد).
ساختار **فقط** از طریق مایگریشن‌های `database/migrations/` ساخته و تغییر داده می‌شود. نسخه فعلی در جدول
`schema_migrations` ثبت است (آخرین نسخه: **006**). پیش از اعمال مایگریشن روی دیتابیس دارای داده،
نسخه پشتیبان در `data/backups/` ساخته می‌شود.

| مایگریشن | محتوا |
| :--- | :--- |
| 001 | جداول پایه: symbols، daily_prices، snapshots، collection_log |
| 002 | data_sources، corporate_actions، market_calendar، intraday_trades، data_quality_metrics، experiments |
| 003 | features_daily، features_intraday |
| 004 | ستون‌های خام و متادیتای نسخه 0.4.x (قبلاً با ALTER دستی اضافه می‌شدند) |
| 005 | شناسه پایدار ins_code، ادغام نمادهای تکراری، اصلاح معنای ستون‌های قیمت، ۵ مظنه کامل، حقیقی/حقوقی روزانه، کدال و قضاوت‌های هوش مصنوعی |
| 006 | ایندکس‌های پرس‌وجوی مقطعی (تاریخ) برای نبض بازار و فیچرهای بازار |

---

## ۱. `symbols` — نمادها
| ستون | نوع | توضیحات |
| :--- | :--- | :--- |
| `id` | INTEGER PK | شناسه داخلی |
| `ins_code` | TEXT UNIQUE | **شناسه پایدار نماد در TSETMC** (insCode) — کلید اصلی هویت |
| `symbol_fa` | TEXT UNIQUE | نماد فارسی یکسان‌سازی‌شده (ی/ک فارسی) |
| `tse_code` | TEXT UNIQUE | کد ISIN (مانند IRO1FOLD0001) |
| `name_fa` | TEXT | نام شرکت |
| `market` | TEXT | `bourse`، `farabourse` یا `farabourse_base` |
| `flow` | INTEGER | کد بازار TSETMC: 1 بورس، 2 فرابورس، 4 پایه |
| `instrument_type_code` | INTEGER | yVal: 300 سهام بورس، 303 سهام فرابورس، 309 پایه، 313 SME |
| `board_code` | TEXT | کد تابلو (N1، N2، Z1، 1Z، ...) |
| `sector_code`، `sector_name` | TEXT | صنعت |
| `shares_outstanding` | REAL | تعداد سهام |
| `base_volume` | INTEGER | حجم مبنا |
| `is_active`، `status` | | نمادی که دیگر در بازار نیست غیرفعال می‌شود ولی داده‌اش حذف نمی‌شود |
| `created_at`، `updated_at`، `last_seen_at` | TEXT | زمان‌ها (تهران) |

## ۲. `daily_prices` — کندل روزانه (Point-in-Time)
| ستون | نوع | توضیحات | در فیچر |
| :--- | :--- | :--- | :---: |
| `symbol_id`، `date` | | `UNIQUE(symbol_id, date, source)` | |
| `open_raw` | REAL | اولین معامله روز (TSETMC: priceFirst) | ✅ |
| `high_raw`، `low_raw` | REAL | بیشترین/کمترین معامله | ✅ |
| `close_raw` | REAL | **آخرین معامله** (pDrCotVal) | ✅ |
| `close_final_raw` | REAL | **قیمت پایانی رسمی** (pClosing) — مبنای دامنه نوسان روز بعد | ✅ |
| `ref_price_raw` | REAL | **قیمت مرجع روز** (priceYesterday) — در روز اقدام شرکتی تعدیل‌شده | ✅ |
| `volume`، `value`، `trade_count` | | حجم، ارزش (ریال)، تعداد معاملات | ✅ |
| `adjustment_factor` | REAL | ضریب اقدام شرکتی همان روز = مرجع ÷ پایانی روز قبل (روز عادی = ۱) | ❌ |
| `adj_close` | REAL | قیمت پایانی تعدیل‌شده رو به عقب — **فقط نمودار** | ❌ |
| `open`، `high`، `low`، `close` | REAL | ستون‌های قدیمی (برابر *_raw) برای سازگاری | — |
| `source` | TEXT | `tsetmc` | |

- روزهای بدون معامله (توقف نماد) کندل ندارند.
- اگر فقط قیمت اولین/آخرین معامله با بازه روز ناسازگار باشد، همان فیلد خالی (NULL) می‌شود و روز حفظ می‌شود.
- **تاریخچه نسخه‌های قبل:** تا نسخه 0.4.2، ستون `adj_close` در واقع قیمت پایانی رسمی خام بود. مایگریشن 005 آن را به `close_final_raw` منتقل کرد.

## ۳. `client_types_daily` — حقیقی/حقوقی روزانه
`buy_i_*` = خرید حقیقی، `buy_n_*` = خرید حقوقی، `sell_i_*` و `sell_n_*` = فروش؛ هر کدام `volume`، `value` (ریال) و `count`.
`UNIQUE(symbol_id, date)`.

## ۴. `snapshots` — اسنپ‌شات لحظه‌ای تابلو
| گروه | ستون‌ها |
| :--- | :--- |
| زمان | `captured_at` (تهران، زمان ثبت محلی)، `provider_timestamp` (زمان سرور)، `last_trade_time` |
| قیمت | `last_price`، `close_price`، `open_price`، `high_price`، `low_price`، `ref_price` |
| دامنه نوسان | `price_limit_up`، `price_limit_down` |
| دفتر سفارش | `bid_price_n`، `bid_vol_n`، `bid_count_n`، `ask_price_n`، `ask_vol_n`، `ask_count_n` برای n = ۱ تا ۵ |
| صف | `buy_queue_volume` (حجم تقاضا وقتی بهترین تقاضا روی سقف است)، `sell_queue_volume` (قرینه) |
| حقیقی/حقوقی تجمعی امروز | `real_buy_volume/count`، `real_sell_*`، `legal_buy_*`، `legal_sell_*` |
| معاملات تجمعی | `volume`، `value`، `trade_count`، `base_volume` |

اسنپ‌شاتی که نسبت به ثبت قبلی همان نماد تغییری نکرده، ذخیره نمی‌شود.

## ۵. `announcements` — اطلاعیه‌های کدال
`tracing_no` (یکتا)، `symbol_fa`، `company_name`، `title`، `letter_code`، `sent_at`، `published_at` (تهران)، `url`.

## ۶. `announcement_judgments` — قضاوت هوش مصنوعی
`announcement_id`، `model` (مثلاً jev-1.13.0)، `questions_version`، `category`، `category_confidence`،
`impact_score` (۰ = به‌شدت منفی تا ۴ = به‌شدت مثبت)، `impact_confidence`، `is_material` (احتمال ۰ تا ۱)،
`answers_json`، `input_tokens`. `UNIQUE(announcement_id, model, questions_version)`.

## ۷. سایر جداول
| جدول | کاربرد |
| :--- | :--- |
| `features_daily`، `features_intraday` | انبار فیچرها (فاز ۱-ب) |
| `intraday_trades` | ریزمعاملات (فاز ۱-ه) |
| `corporate_actions` | اقدامات شرکتی (ضرایب از `daily_prices.adjustment_factor` قابل استخراج است) |
| `market_calendar` | تعطیلات رسمی بازار |
| `data_quality_metrics` | ناهنجاری‌ها و اسنپ‌شات‌های ردشده |
| `experiments` | ثبت علمی بکتست‌ها و مدل‌ها (نسخه داده/فیچر/مدل، پارامترها، نتایج) |
| `collection_log` | گزارش هر اجرای گردآوری |
| `data_sources` | فهرست منابع داده |
| `schema_migrations` | نسخه‌های مایگریشن اعمال‌شده |
