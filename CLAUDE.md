@AGENTS.md

# راهنمای Claude Code برای پروژه BRS

## کاربر
- معامله‌گر بورس ایران، **برنامه‌نویس نیست** (Vibe Coder). توضیح‌ها را به فارسی ساده، کوتاه و بدون اصطلاح فنی غیرضروری بنویس.
- از VPN + Proxifier استفاده می‌کند. اگر سایت ایرانی (tsetmc.com، codal.ir، irfarabi.com) باز نشد، به او بگو دامنه را در Proxifier روی Direct بگذارد.
- کارگزاری: فارابی (فارابیکسو). **هرگز سفارش خرید/فروش ثبت نکن و وارد حساب کارگزاری نشو.**
- توصیه سرمایه‌گذاری شخصی نده؛ ابزار تحلیل و بکتست بساز.

## دستورات
```bash
venv/Scripts/python.exe -m pytest -m "not network"   # آزمون‌های آفلاین (قبل از هر commit)
venv/Scripts/python.exe -m pytest -m network         # آزمون با اینترنت واقعی
venv/Scripts/ruff.exe check . --fix                  # کیفیت کد
venv/Scripts/python.exe brs.py doctor                # عیب‌یابی محیط، شبکه، دیتابیس، Jev (--json logs/doctor.json برای گزارش ماشین‌خوان)
venv/Scripts/python.exe brs.py update --symbols فولاد  # به‌روزرسانی داده (بدون --symbols: کل بازار، ~۴۵ دقیقه بار اول)
venv/Scripts/python.exe brs.py backup                # پشتیبان دستی دیتابیس (۵ نسخه آخر)
venv/Scripts/python.exe brs.py features              # ساخت انبار فیچر (بعد از هر update خودکار)
venv/Scripts/python.exe brs.py research              # برچسب‌های خالص + ارزیابی استراتژی‌های پایه (فاز ۱-ج، ~۶ دقیقه)
```
- داشبورد: پیش‌نمایش `brs-dashboard` در `.claude/launch.json` (یا START.bat برای کاربر).
- وابستگی جدید: به `requirements.txt` اضافه کن، سپس `uv pip compile requirements.txt --python-version 3.11 --universal -o requirements.lock.txt` و `uv pip sync --python venv/Scripts/python.exe requirements.lock.txt`.

## نقشه کد
| مسیر | مسئولیت |
| :--- | :--- |
| `core/` | منطق خالص دامنه: `market_rules` (کارمزد، صف قفل، بازده PIT)، `text` (ی/ک عربی)، `timeutils` (تهران، شمسی) |
| `data_collector/tsetmc.py` | تنها نقطه تماس با TSETMC (پیاده‌سازی `BaseDataProvider`) |
| `data_collector/scheduler.py` | `DataCollector`: هماهنگی گردآوری و ثبت در دیتابیس |
| `database/db.py` + `migrations/` | دسترسی به SQLite؛ **هر تغییر ساختار = فایل مایگریشن جدید با شماره بعدی** (پشتیبان خودکار گرفته می‌شود) |
| `backtest/engine.py` | بکتست: اجرای روز بعد، VWAP، صف قفل، کارمزد از config |
| `features/` | موتور فیچر PIT (`daily`, `market`) و انبار فیچر Parquet نسخه‌دار (`store`)؛ نام فارسی فیچرها در `labels_fa` — هر تغییر منطق = `FEATURE_SET_VERSION` جدید + آزمون بریدن داده |
| `research/` | فاز ۱-ج: `labels` (بازده خالص قابل اجرا، نسخه‌دار)، `baselines` (استراتژی‌های پایه، معیار تصادفی، t نیوی-وست)، `run` (ثبت آزمایش و گزارش). دوره `holdout_start` مهر و موم است — نتایج: `docs/research/` |
| `ai/` | Jev (TypeSafe) — کلید در `.env`؛ پلاگین `typesafe` راهنمای ساخت با آن است |
| `dashboard/app.py` | رابط فارسی راست‌چین؛ کارهای طولانی با `ops/jobs.py` در پس‌زمینه |
| `ops/doctor.py` | عیب‌یاب با راه‌حل فارسی |
| `docs/phase1_design.md` | طراحی و نقشه راه فاز ۱ — قبل از شروع قابلیت جدید بخوان |

## قواعد کار
- فقط یک شاخه دائمی: `master`. هر کار روی شاخه موقت `feature/*` ← PR به `master` ← ادغام پس از سبز شدن CI ← حذف شاخه.
  کاربر اختیار commit/push/ادغام را داده است (۲۰ مهر ۱۴۰۵)؛ force-push و ادغام با CI قرمز هرگز.
- هر قابلیت جدید = آزمون در `tests/` (داده آزمون در `tests/conftest.py`، بدون اینترنت). آزمون‌هایی که اینترنت لازم دارند با `@pytest.mark.network`.
- متن رابط کاربری و پیام‌های خطا فارسی؛ کامنت‌های کد فارسی (مطابق سبک موجود).
- برای فیچرها فقط ستون‌های `*_raw` و `ref_price_raw`؛ بازده = `market_rules.pit_daily_returns`.
- بعد از تغییر بنیادی، `docs/` را به‌روز کن.
