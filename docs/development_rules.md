# راهنمای توسعه و انضباط کدنویسی BRS (Development Rules)

## ۱. استراتژی شاخه‌ها (Git Branch Strategy)
- شاخه `master` (یا `main`): شاخه کد پایدار و عملیاتی. کامیت مستقیم روی این شاخه در کارهای بزرگ ممنوع است.
- شاخه `develop`: شاخه اصلی ادغام کارهای جدید قبل از ریلیز.
- شاخه‌های `feature/*`: برای پیاده‌سازی فیچرهای مستقل (مانند `feature/backtest-engine`).

## ۲. ردیابی کارها با Issueها (Task Tracking)
تمامی تغییرات و فیچرهای پروژه به یک Issue در مخزن گیت‌هاب نگاشت می‌شوند.
- `BRS-001`: Market Universe Pipeline
- `BRS-002`: Intraday Storage & Tick Architecture
- `BRS-003`: Quant Validation & Anomaly Detection
- `BRS-004`: Backtesting Engine & Risk Model
- `BRS-005`: Feature Engineering Framework
- `BRS-006`: ML Model Training & Evaluation Pipeline

## ۳. استاندارد آزمون‌ها (CI / CD)
- با هر پوش روی گیت‌هاب، اکشن `tests.yml` اجرا شده و کل تست‌های `pytest` را ارزیابی می‌کند.
- در صورت شکست تست‌ها، کد پذیرفته نمی‌شود.
