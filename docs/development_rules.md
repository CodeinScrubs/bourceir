# راهنمای توسعه و انضباط کدنویسی BRS (Development Rules)

## ۱. استراتژی شاخه‌ها (Git Branch Strategy)
- **فقط یک شاخه دائمی: `master`** (تصمیم کاربر، ۲۰ مهر ۱۴۰۵). همیشه پایدار و آزموده است.
- هر کار در یک شاخه **موقت** `feature/*` انجام می‌شود، با Pull Request به `master` ادغام می‌شود و بلافاصله پس از
  ادغام خودکار حذف می‌شود (تنظیم «Automatically delete head branches» در GitHub روشن است). پس در حالت عادی فقط
  `master` وجود دارد.
- کامیت مستقیم روی `master` ممنوع است: هر تغییر باید از CI (ویندوز و لینوکس) سبز عبور کند.

## ۲. ردیابی کارها با Issueها
تمامی تغییرات به یک Issue در مخزن گیت‌هاب نگاشت می‌شوند:
- `BRS-001`: Market Universe Pipeline ← فاز ۱-الف ✅
- `BRS-002`: Intraday Storage & Tick Architecture ← اسنپ‌شات ✅، ریزمعاملات در فاز ۱-ه
- `BRS-003`: Quant Validation & Anomaly Detection ← پایه ✅
- `BRS-004`: Backtesting Engine & Risk Model ← موتور ✅، برچسب و استراتژی پایه در فاز ۱-ج
- `BRS-005`: Feature Engineering Framework ← فاز ۱-ب
- `BRS-006`: ML Model Training & Evaluation Pipeline ← فاز ۱-د

## ۳. چرخه کار با هوش مصنوعی (Vibe Coding)
1. درخواست را به فارسی و با یک هدف مشخص بنویسید.
2. ایجنت روی شاخه `feature/*` کد و آزمون می‌نویسد و `pytest -m "not network"` و `ruff check .` را اجرا می‌کند.
3. نتیجه در داشبورد بررسی می‌شود.
4. کاربر اختیار commit، push و ادغام را به ایجنت داده است (۲۰ مهر ۱۴۰۵): پس از سبز شدن همه آزمون‌ها و CI،
   Pull Request به `master` ادغام و شاخه موقت حذف می‌شود. force-push و ادغام با CI قرمز هرگز مجاز نیست.

## ۴. استاندارد آزمون‌ها (CI / CD)
- با هر push یا Pull Request، اکشن `tests.yml` روی Python 3.11 (با منطقه زمانی UTC، تا وابستگی به ساعت محلی آشکار شود) اجرا می‌شود: `ruff check .` و همه آزمون‌های آفلاین.
- آزمون‌های نیازمند اینترنت (`@pytest.mark.network`) فقط به‌صورت دستی اجرا می‌شوند: `pytest -m network`.
- اگر آزمون‌ها شکست بخورند، کد پذیرفته نمی‌شود.
