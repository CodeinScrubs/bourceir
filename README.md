# BRS Quant Engine — بورس ایران (bourceir)

> **سامانه مهندسی داده و نوسان‌گیری هوشمند در بازار بورس و فرابورس تهران (TSE & IFB)**

[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.14-blue.svg)](https://www.python.org/)
[![Database](https://img.shields.io/badge/database-SQLite-green.svg)](https://www.sqlite.org/)
[![Dashboard](https://img.shields.io/badge/dashboard-Streamlit-red.svg)](https://streamlit.io/)
[![Tests](https://img.shields.io/badge/tests-pytest%20passing-brightgreen.svg)](https://pytest.org/)

---

## 🎯 هدف پروژه
پروژه **BRS** با هدف ایجاد یک پلتفرم جامع معاملات الگوریتمی (Quant Trading) و نوسان‌گیری با هوش مصنوعی برای بازار سرمایه ایران توسعه می‌یابد. این سیستم از دو موتور مجزا تشکیل می‌شود:
1. **موتور نوسان‌گیری درون‌روزی (Intraday Scalp):** رصد مداوم صفوف خرید/فروش، قدرت خریدار به فروشنده، ورود پول هوشمند و شناسایی نقاط ورود/خروج روزانه.
2. **موتور نوسان‌گیری کوتاه‌مدت (Swing ۲ تا ۱۰ روزه):** تحلیل رفتار قیمتی، شکست سطوح تکنیکال و رتبه‌بندی نمادهای مستعد رشد.

---

## 🏗️ معماری نرم‌افزار

```mermaid
flowchart TD
    subgraph Data Layer [لایه گردآوری داده]
        TSE[TSETMC Data Source] --> BaseProvider[BaseDataProvider Interface]
        BaseProvider --> PytseProv[TSEDataProvider Implementation]
        PytseProv --> Validator[DataValidator Quality Gate]
    end

    subgraph Storage [پایگاه داده SQLite]
        Validator --> DB[(SQLite: data/brs.db)]
        DB --> T1[symbols: مشخصات نمادها]
        DB --> T2[daily_prices: کندل‌های OHLCV]
        DB --> T3[snapshots: صفوف و داده‌های تابلو]
        DB --> T4[collection_log: پایش سلامت سیستم]
    end

    subgraph UI & Analytics [لایه تحلیل و نمایش]
        DB --> Dashboard[Streamlit Web Dashboard]
        DB --> Backtest[Backtester Engine (آتی)]
        DB --> ML[Feature & AI Engine (آتی)]
    end
```

---

## 📁 ساختار پوشه‌بندی پروژه

```
bourse-prediction/
├── config/
│   ├── __init__.py
│   └── settings.py          # تنظیمات متمرکز سیستم، ساعات بازار و نمادها
├── data_collector/
│   ├── __init__.py
│   ├── base.py              # اینترفیس پایه (BaseDataProvider) جهت استقلال از پکیج‌های ثالث
│   ├── validator.py         # لایه اعتبارسنجی کیفیت و سلامت داده‌ها (DataValidator)
│   ├── universe.py          # فیلتر و مدیریت کل جامعه سهام عادی بازار (UniverseManager)
│   ├── provider.py          # تامین‌کننده داده‌های TSETMC مبتنی بر BaseDataProvider
│   └── scheduler.py         # زمان‌بندی و چرخه دریافت دوره‌ای داده‌ها
├── database/
│   ├── __init__.py
│   ├── models.py            # اسکیما و جداول SQL با کلیدهای یکتا و شاخص‌ها
│   └── db.py                 # کلاس DatabaseManager با مدیریت کانکشن و تراکنش
├── dashboard/
│   ├── __init__.py
│   └── app.py               # داشبورد تحلیلی استریم‌لیت با نمایش گرافیکی و جداول
├── tests/
│   ├── __init__.py
│   └── test_provider.py     # تست‌های جامع واحد (۷ تست موفق در pytest)
├── run_collector.py         # نقطه ورود خط فرمان گردآوری داده‌ها
├── app.py                  # نقطه ورود مستقیم داشبورد
├── run_dashboard.bat       # اجرای داشبورد با دو کلیک در ویندوز
├── run_collector.bat       # اجرای گردآوری داده با دو کلیک در ویندوز
├── requirements.txt         # لیست وابستگی‌های پروژه
└── README.md
```

---

## 🚀 راهنمای راه‌اندازی و اجرا

### ۱. فعال‌سازی محیط مجازی
```powershell
.\venv\Scripts\Activate.ps1
```

### ۲. اجرای تست‌های خودکار
```powershell
python -m pytest tests/ -v
```

### ۳. دریافت یا به‌روزرسانی داده‌های بورس
```powershell
# بررسی وضعیت ارتباط با سرور بورس
python run_collector.py --mode test

# دریافت تاریخچه روزانه نمادها
python run_collector.py --mode history --symbols خودرو,فولاد,شپنا,وبملت,کگل

# ثبت اسنپ‌شات لحظه‌ای تابلو
python run_collector.py --mode snapshot
```

### ۴. اجرای داشبورد تحت وب
```powershell
streamlit run app.py
```
سپس مرورگر خود را در آدرس `http://localhost:8501` باز کنید.

---

## 🛡️ اصول بنیادین مهندسی داده در BRS

1. **عدم جعل داده (Zero Mock in Production):** در صورت قطع ارتباط سرور، سیستم هرگز داده ساختگی ثبت نمی‌کند و خطا در لاگ مستند می‌شود.
2. **استقلال لایه داده (Provider Abstraction):** با طراحی `BaseDataProvider`، تغییر یا افزودن منابع داده بدون دستکاری هسته انجام می‌شود.
3. **لایه اعتبارسنجی (Data Validation):** هر کندل از نظر قوانین قیمت (High >= Low, Price > 0) و عدم منفی بودن حجم بررسی و پالایش می‌شود.
4. **مدیریت جامعه سهام (Universe Screening):** تفکیک خودکار سهام عادی از صندوق‌ها، اوراق بدهی، حق‌تقدم و ابزارهای مشتقه.
5. **جلوگیری از داده‌های تکراری:** محدودیت‌های `UNIQUE` در دیتابیس مانع از درج رکوردهای تکراری می‌شود.
