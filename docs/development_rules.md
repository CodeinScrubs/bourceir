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
- `BRS-009` ([Issue #24](https://github.com/CodeinScrubs/bourceir/issues/24)): تعریف و ممیزی فیلترهای جریان حقیقی، پژوهش سرمایه مشترک و نمایش شروط نامعلوم؛ پذیرش سیگنال همچنان غیرفعال است.
- `BRS-010` ([Issue #26](https://github.com/CodeinScrubs/bourceir/issues/26)): راستی‌آزمایی پیشنهادهای AI، بهای کسر سهم در موتور قدیمی، تقویم ثبت‌شده، اتصال فقط‌خواندنی بازپخش و حذف پردازش تکراری فیلترها.
- `BRS-011` ([Issue #28](https://github.com/CodeinScrubs/bourceir/issues/28)): بررسی شواهد قاسم، مقایسه حجم در مرز مرجع، جستجوی یکتا، دریافت هدفمند کدال و وضوح نمودار/بهای معاملات.
- `BRS-012` ([Issue #30](https://github.com/CodeinScrubs/bourceir/issues/30)): راستی‌آزمایی پیوست نسخه ۰٫۶٫۶، حفظ نامعلوم در ریسک/پژوهش، خواندن و نصب فارابی، هویت نماد و کنترل ZIP ویندوز.
- `BRS-013` ([Issue #32](https://github.com/CodeinScrubs/bourceir/issues/32)): پروتکل و ثبت مستقل چرخه دریافت درون‌روزی؛ تشخیص غیبت/تکرار شناسه، وقفه، پایان نامعلوم و تفکیک دریافت از تغییر اسنپ‌شات.

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
