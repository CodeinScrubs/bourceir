# پایپ‌لاین و چرخه داده‌های BRS (Data Pipeline)

> به‌روزرسانی بازبینی ۲۰۲۶-۱۰-۱۱: قرارداد جدید، محدودیت‌ها و رفتار اصلاح‌شده در [foundation_review_fa.md](foundation_review_fa.md) ثبت شده است. مسیر نصب فعلی [windows_phase1_fa.md](windows_phase1_fa.md) است. ادعاهای اجرای قبلی این سند در این بازبینی مستقلاً تأیید نشده‌اند.


## ۱. چرخه حیات داده

```mermaid
sequenceDiagram
    participant User as کاربر (داشبورد / brs.py)
    participant Collector as DataCollector
    participant Provider as TsetmcProvider / CodalProvider
    participant Validator as DataValidator
    participant DB as SQLite

    User->>Collector: update / live / codal
    Collector->>Provider: درخواست (با تلاش مجدد خودکار و محدودیت نرخ)
    alt پاسخ موفق
        Provider->>Validator: پالایش نحوی (حذف روز خراب / خالی کردن فیلد ناسازگار)
        Validator-->>Collector: داده سالم + پرچم ناهنجاری کمی
        Collector->>DB: UPSERT (بدون رکورد تکراری) + محاسبه ضرایب تعدیل
        Collector->>DB: ناهنجاری‌ها → data_quality_metrics
    else خطا یا عدم پاسخ
        Provider-->>Collector: None (هرگز داده ساختگی)
        Collector->>DB: ثبت خطا در collection_log
    end
```

## ۲. کارهای گردآوری

| کار | دستور | منبع | توضیح |
| :--- | :--- | :--- | :--- |
| جامعه سهام | `brs.py universe` | دیده‌بان کامل TSETMC | فیلتر yVal ∈ {300, 303}، بدون تابلوی بلوکی |
| کندل امروز کل بازار | `brs.py update` | `GetMarketWatch` + `GetClosingPriceInfo` | یک درخواست، بعد از ساعت ۱۳؛ تاریخ از آخرین روز معاملاتی رسمی |
| تاریخچه روزانه | `brs.py update` | `GetClosingPriceDailyList` | بار اول کامل؛ بعد ۳۰ روز اخیر (افزایشی). TSETMC کندل امروز را تا عصر به‌تدریج منتشر می‌کند؛ رکورد دیده‌بان با داده رسمی بازنویسی می‌شود |
| حقیقی/حقوقی | `brs.py update` | `GetClientTypeHistory` | بار اول کامل؛ بعد فقط روزهای جدید |
| کدال | `brs.py codal --judge` | جستجوی کدال | فقط نمادهای داخل دامنه + قضاوت Jev |
| پایش زنده | `brs.py live` | `GetMarketWatch` + `GetClientTypeAll` | ۲ درخواست برای کل بازار در هر چرخه ۳۰ ثانیه‌ای |

## ۳. سطوح اعتبارسنجی

### ۳.۱. نحوی
- قیمت‌های بیشترین/کمترین/پایانی مثبت، `High ≥ Low`، حجم غیرمنفی ← در غیر این صورت روز حذف می‌شود.
- قیمت اولین/آخرین معامله خارج از بازه روز ← فقط همان فیلد NULL می‌شود.
- اسنپ‌شات: زمان ثبت الزامی، آخرین قیمت در دامنه مجاز روز، قیمت صفر با وجود معامله ممنوع ← اسنپ‌شات رد و در `data_quality_metrics` ثبت می‌شود.

### ۳.۲. کمی (علامت‌گذاری بدون حذف)
- تغییر قیمت پایانی نسبت به قیمت مرجع بیش از ۱۰.۵٪ (معمولاً روز بازگشایی پس از توقف طولانی).
- حجم بیش از ۱۰ برابر میانه ۳۰ روز قبل.
