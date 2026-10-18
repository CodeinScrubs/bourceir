@AGENTS.md

# راهنمای Claude Code برای پروژه BRS

## کاربر
- معامله‌گر بورس ایران، **برنامه‌نویس نیست** (Vibe Coder). توضیح‌ها را به فارسی ساده، کوتاه و بدون اصطلاح فنی غیرضروری بنویس.
- از VPN + Proxifier استفاده می‌کند. اگر سایت ایرانی (tsetmc.com، codal.ir، irfarabi.com) باز نشد، به او بگو دامنه را در Proxifier روی Direct بگذارد.
- کارگزاری: فارابی (فارابیکسو). **هرگز سفارش خرید/فروش ثبت نکن.** مالک برداشت فقط‌خواندنی از نشست مجاز مرورگر و خروجی رسمی گزارش‌ها را درخواست کرده است؛ رمز/توکن ذخیره نمی‌شود و API سفارش استفاده نمی‌شود. اطلاعات واقعی حساب فقط محلی و خصوصی است؛ راهنما: `docs/farabi-guide.md`.
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
venv/Scripts/python.exe brs.py research-dip          # آزمون روش کاربر: خرید در اصلاح روز بعد + فروش با هدف سود (~۱۵ دقیقه)
```
- داشبورد: پیش‌نمایش `brs-dashboard` در `.claude/launch.json` (یا START.bat برای کاربر).
- وابستگی جدید: به `requirements.txt` اضافه کن، سپس `uv pip compile requirements.txt --python-version 3.11 --universal -o requirements.lock.txt` و `uv pip sync --python venv/Scripts/python.exe requirements.lock.txt`.

## نقشه کد

مسیر جدید 0.7.17: `core/capture_evidence.py` پیوند همه ستون‌های snapshot را با چرخه دریافت کنترل می‌کند؛ `capture-protocol-v2` و `forward-observation-v2` شکست تازه، تغییر محتوا، کد/هویت و ساعت را مستقل کنترل می‌کنند. گزارش v1 خواندنی است؛ ادامه با کد تازه نیاز به پایش و برنامه تازه دارد. این تطبیق محلی، صحت بیرونی/اجرای معامله/سود نیست. [قرارداد](docs/research/receipt-bound-observations-fa.md).

مسیر جدید 0.7.16: `research/flow_v4_run.py` و `core/flow_filters.py` خانواده مستقل v4 را با سه کنترل تصادفی/دو اجرا و bootstrap محفوظ ارزیابی می‌کنند؛ تعریف v3 عوض نمی‌شود. `core/dip_diagnostics.py` + `ops/dip_diagnostics.py` فقط آرشیو DIP را کالبدشکافی می‌کنند. `ops/operator_status.py` + `dashboard/operator_status.py` نمای محلی مشترک دو دفترند. `codal-normalize` محدود به جدول با واحد/دوره صریح است؛ هویت/دسترسی تاریخی را حدس نمی‌زند. [قرارداد](docs/research/flow-v4-protocol-fa.md).

| مسیر | مسئولیت |
| :--- | :--- |
| `research/probability.py` + `research/probability_run.py` + `dashboard/probability.py` | آزمایش `temporal-frequency-v1` از منبع BASELINES صریح؛ فراوانی گذشته با purge واقعی/embargo، پیش‌بینی محفوظ قبل از نتیجه، Brier زوجی و نمودار کالیبراسیون، کران نامعلوم و همه ۱۶ قاعده/افق. `core/probability.py` سناریوی دستی هزینه/احتمال است. احتمال اختصاصی سهم یا مجوز معامله ندارد؛ [راهنما](docs/research/probability-lab-fa.md). |
| `core/market_review.py` + `ops/market_review.py` + `dashboard/market_review.py` | گزارش توصیفی جلسه با نوع ابزار و تاریخ مستقل هر سهم، شاخص/صنعت/پوشش، برنامه و شواهد محفوظ، بازحساب آفلاین با زمان اصلی؛ `market-review-v1`. صف واقعی و خالص ارزش حقیقی نامعلوم از کندل/حجم ساخته نمی‌شود؛ [راهنما](docs/research/current-market-review-fa.md). `core/market_summary.py` پوشش و unknown را در خانه مشترک می‌کند. |
| `core/query_screener.py` + `ops/query_screener.py` + `dashboard/query_screener.py` | پرسش توصیفی ثبت‌شده پیش از خواندن، رتبه ارزش بازار جاری با کران نامعلوم/هم‌ارزش، ترتیب منفی→مثبت با اثبات کندلی محدود و تیک تطبیق‌شده؛ `market-query-v4`. چند نسخه ابطال‌شده در مسیر فعال نیست؛ تعارض فعال/ابطال یا مجموع/مسیر کامل، قطعیت نمی‌سازد. سیگنال/اجرای سفارش/جامعه تاریخی PIT نیست. [راهنما](docs/research/market-queries.md). |
| `data_collector/trade_archive.py` + `data_collector/public_site.py` + `ops/source_catalog.py` | خام فشرده ریزمعاملات، برداشت قابل ادامه سایت عمومی و فهرست ادعاها بدون اجرای کد ناشناس. فقط provider با TSETMC حرف می‌زند. [راهنما](docs/algosaham-public-archive.md). |
| `core/` | منطق خالص دامنه: `market_rules` (کارمزد، صف قفل، بازده PIT)، `text` (ی/ک عربی)، `timeutils` (تهران، شمسی) |
| `core/paper.py` + `database/paper.py` + `dashboard/paper.py` | پورتفوی مجازی مستقل با سرمایه دلخواه یا کپی موجودی آغازین از گزارش تازه فارابی؛ خرید گذشته ساخته نمی‌شود. نقد مشترک، بهای هر تحلیل و منبع محفوظ‌اند؛ ابزار خارج از دامنه فقط مشاهده می‌شود. سناریوی دستی جدا از مظنه تازه است. Migration 013/014، پروتکل v2 با خواندن v1؛ داده/اقدام شرکتی نامعلوم مانع سود عددی است. هیچ سفارش واقعی یا سیگنال پذیرفته‌شده ندارد. [راهنما](docs/paper-portfolio.md). |
| `data_collector/tsetmc.py` | تنها نقطه تماس با TSETMC (پیاده‌سازی `BaseDataProvider`) |
| `data_collector/scheduler.py` | `DataCollector`: هماهنگی گردآوری و ثبت در دیتابیس |
| `database/db.py` + `migrations/` | دسترسی به SQLite؛ **هر تغییر ساختار = فایل مایگریشن جدید با شماره بعدی** (پشتیبان خودکار گرفته می‌شود) |
| `backtest/engine.py` | بکتست: اجرای روز بعد، VWAP، صف قفل، کارمزد از config |
| `features/` | موتور فیچر PIT (`daily`, `market`) و انبار فیچر Parquet نسخه‌دار (`store`)؛ نام فارسی فیچرها در `labels_fa` — هر تغییر منطق = `FEATURE_SET_VERSION` جدید + آزمون بریدن داده |
| `research/` | daily-v7، labels-v4، dip-v4 با خلاصه dip-summary-v2 و portfolio-v4؛ کسر مصنوعی سهم با قیمت روز و اقدام بعدی ارزش‌گذاری می‌شود و نقد قابل خرج نیست. `portfolio_run` پژوهش سرمایه مشترک از ورودی پایه محفوظ، `walkforward` برش زمانی و purge با تاریخ خروج واقعی. آمار روز یا قرعه نامعلوم صفر یا از مخرج حذف نمی‌شود. نتیجه پژوهشی است و سیگنال پذیرفته‌شده نداریم. دوره `holdout_start` مهر و موم است. پروتکل سه افق: `docs/research/personal_strategy_protocol.md` |
| `ai/` | Jev (TypeSafe) — کلید در `.env`؛ پلاگین `typesafe` راهنمای ساخت با آن است |
| `dashboard/app.py` | رابط فارسی راست‌چین؛ کارهای طولانی با `ops/jobs.py` در پس‌زمینه |
| `ops/doctor.py` + `ops/trader_readiness.py` | عیب‌یاب با راه‌حل فارسی و حالت صریح `--read-only`؛ گزارش آمادگی آفلاین فقط از بازار، بدون محتوای حساب یا پذیرش روش. [راهنما](docs/trader-readiness.md). |
| `ops/environment.py` + `ops/report_files.py` | تطبیق همه نسخه‌های قفل‌شده/رسید نصب در Windows و انتشار اتمی گزارش خصوصی بدون بازنویسی ورودی. |
| `ops/research_preflight.py` + `research/uncertainty.py` + `dashboard/reliability.py` | بررسی فقط‌خواندنی قرارداد داده توسعه و نمایش گزارش محفوظ در سلامت سامانه؛ اختلاف زوجی روزانه با بازنمونه‌گیری بلوکی و مخرج نامعلوم. پذیرش سیگنال یا احتمال سود نیست. [راهنما](docs/research/reliability-roadmap-fa.md). |
| `core/price_quality.py` + `ops/price_recheck.py` | علت‌های مشخص نقص قیمت و تفکیک تاریخچه پیشین در preflight-v3؛ برنامه بازبینی پیش از دریافت، کد محفوظ و مقایسه محدود فقط از provider. هیچ اصلاح خودکار ندارد. [مراحل فعلی](docs/research/next-phase-plan-fa.md) مرجع تصمیم مرحله بعد است؛ طرح اولیه فاز۱ سند تاریخی است. |
| `core/price_recovery.py` + `ops/price_recovery.py` + `database/price_recovery.py` | بازیابی اولین/آخرین معامله گمشده فقط از مسیر کامل فعال با تطبیق دقیق آمار روز؛ برنامه پیشین، کد و آرشیو محفوظ. اعمال صریح با پشتیبان و رسید 015؛ دریافت یکسان بعدی شاهد را پاک نمی‌کند و تغییر آمار آن را معتبر فرض نمی‌کند. `raw-inputs-v2` رسیدها را در منشأ فیچر ثبت می‌کند. صحت مستقل/دسترسی تاریخی/سود اثبات نمی‌شود. [راهنما](docs/research/price-recovery.md). |
| `core/research_review.py` + `ops/research_review.py` | بازبینی فقط‌خواندنی آرشیو سرمایه مشترک/جریان: تطبیق ثبت/هش و محاسبه دوباره آمار منحنی، مخرج نامعلوم و مقایسه زوجی. بازاجرای موتور یا مجوز سیگنال نیست؛ راهنما `docs/research/research-review.md` و قرارداد/۷۶ پرسش `docs/research/builder-brief-fa.md`. |
| `core/stock_study.py` + `ops/stock_study.py` | مطالعه توصیفی با ورودی و کد محفوظ، همتای منتخب فقط از اطلاعات پیشین و رتبه ارزش بازار مشاهده؛ `stock-study-v1`. نمونه و دوره بررسی‌شده برای فرضیه مشتق‌شده مستقل نیست؛ مجوز سیگنال ندارد. راهنما `docs/research/stock-study.md`. فیلتر جریان `flow-filters-v3` / `flow-portfolio-v3` است؛ کنترل سازگاری آن با `daily-v7` در `core/client_quality.py` مشترک است. |
| `core/codal_documents.py` + `ops/codal_documents.py` | آرشیو متن/PDF عمومی کدال، نسخه مستقل و هش محتوا/کد؛ زمان دسترسی زودتر از دریافت/حفظ نیست. تأیید عددی/هویت/انتشار تاریخی یا ورودی خودکار مدل ندارد. راهنما `docs/codal-documents.md`. |
| `core/codal_tables.py` + `ops/codal_tables.py` | سلول‌های JSON اولیه گزارش یک‌ماهه نوع ۵۸، بدون اجرای JavaScript/فرمول؛ `codal-monthly-cells-v1`. Decimal پیشنهادی بدون واحد تأییدشده است؛ صفر/نامعلوم جدا و هر دریافت با کد/هش مستقل محفوظ. به مدل یا دفتر وارد نمی‌شود. راهنما `docs/codal-tables.md`. |
| `database/capture.py` + `core/capture_evidence.py` + `ops/capture_audit.py` | پروتکل فقط‌افزودنی دامنه/کد پیش از دریافت؛ `capture-protocol-v2` هش محتوای ردیف را حفظ و `capture-quality-v2` پیوند/تعارض را جدا از تازگی زمان قیمت گزارش می‌کند. ثبت پایش، ثبت تصمیم پیش از نتیجه نیست. |
| `core/forward_observations.py` + `database/forward_observations.py` + `dashboard/forward_observations.py` | دفتر مشاهده دستی آینده با دامنه/پنجره ثابت، کپی کد/قیمت و چرخه دریافت مشترک (`forward-observation-v2`، جدول‌های migration 012). v1 خواندنی است؛ ثبت محلی، اجرا/سود مالی یا آزمون مستقل را اثبات نمی‌کند. راهنما `docs/research/forward-observations.md`. |
| `docs/phase1_design.md` | طراحی و نقشه راه فاز ۱ — قبل از شروع قابلیت جدید بخوان |

## قواعد کار
- فقط یک شاخه دائمی: `master`. هر کار روی شاخه موقت `feature/*` ← PR به `master` ← ادغام پس از سبز شدن CI ← حذف شاخه.
  کاربر اختیار commit/push/ادغام را داده است (۲۰ مهر ۱۴۰۵)؛ force-push و ادغام با CI قرمز هرگز.
- هر قابلیت جدید = آزمون در `tests/` (داده آزمون در `tests/conftest.py`، بدون اینترنت). آزمون‌هایی که اینترنت لازم دارند با `@pytest.mark.network`.
- متن رابط کاربری و پیام‌های خطا فارسی؛ کامنت‌های کد فارسی (مطابق سبک موجود).
- برای فیچرها فقط ستون‌های `*_raw` و `ref_price_raw`؛ بازده = `market_rules.pit_daily_returns`.
- بعد از تغییر بنیادی، `docs/` را به‌روز کن.
