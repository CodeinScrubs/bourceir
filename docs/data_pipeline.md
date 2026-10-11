# پایپ‌لاین و چرخه داده‌های BRS (Data Pipeline)

## ۱. چرخه حیات داده

```mermaid
sequenceDiagram
    participant Scheduler as اسکلولر زمان‌بندی
    participant Provider as تامین‌کننده داده (TSEDataProvider)
    participant Validator as لایه اعتبارسنجی (DataValidator)
    participant DB as دیتابیس SQLite

    Scheduler->>Provider: درخواست تاریخچه یا اسنپ‌شات
    Provider->>Provider: اعمال محدودیت نرخ درخواست (Rate Limit)
    Provider-->>Scheduler: ارسال دیتافریم خام
    Scheduler->>Validator: بررسی صحت ریاضی و بازار (Quant Check)
    alt داده سالم است
        Validator-->>Scheduler: تأیید رکوردها
        Scheduler->>DB: ثبت با شرط عدم تکرار (INSERT OR IGNORE)
    else داده دارای خطا است
        Validator-->>Scheduler: اخطار و فیلتر رکوردهای خراب
        Scheduler->>DB: ثبت در لاگ خطاها
    end
```

## ۲. سطوح اعتبارسنجی داده

### ۲.۱. اعتبارسنجی نحوی (Syntax Validation)
- قیمت‌ها مثبت و بزرگتر از صفر باشند.
- حجم و ارزش معاملات غیرمنفی باشند.
- رابطه ریاضی کندل حفظ شود: $High \ge Low$ و $High \ge Open, Close$ و $Low \le Open, Close$.

### ۲.۲. اعتبارسنجی کمی و مالی (Quant Anomaly Validation)
- جهش‌های قیمتی غیرعادی خارج از دامنه نوسان مجاز روزانه شناسایی و علامت‌گذاری شوند.
- حجم‌های انفجاری غیرطبیعی (بیش از ۱۰ برابر میانگین ۳۰ روزه) تفکیک شوند.
