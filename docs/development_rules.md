# راهنمای توسعه و انضباط کدنویسی BRS (Development Rules)

## ۱. استراتژی شاخه‌ها (Git Branch Strategy)
- `master`: کد پایدار. کامیت مستقیم در کارهای بزرگ ممنوع است.
- `develop`: شاخه ادغام کارهای جدید قبل از انتشار.
- `feature/*`: هر قابلیت در شاخه جدا (مانند `feature/phase1-foundation`، `feature/features-daily`).

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
4. commit و Pull Request به `develop` با درخواست کاربر انجام می‌شود.

## ۴. استاندارد آزمون‌ها (CI / CD)
- با هر push یا Pull Request، اکشن `tests.yml` روی Python 3.11 (با منطقه زمانی UTC، تا وابستگی به ساعت محلی آشکار شود) اجرا می‌شود: `ruff check .` و همه آزمون‌های آفلاین.
- آزمون‌های نیازمند اینترنت (`@pytest.mark.network`) فقط به‌صورت دستی اجرا می‌شوند: `pytest -m network`.
- اگر آزمون‌ها شکست بخورند، کد پذیرفته نمی‌شود.
