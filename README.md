# BRS Data Engine v0.1
# سیستم هوشمند نوسان‌گیری بورس ایران

## 🎯 هدف
جمع‌آوری، ذخیره و نمایش داده‌های بازار بورس ایران (سهام بورس و فرابورس) به عنوان زیرساخت سیستم هوشمند نوسان‌گیری.

## 📁 ساختار پروژه
```
bourse-prediction/
├── config/
│   └── settings.py          # تنظیمات مرکزی
├── data_collector/
│   ├── __init__.py
│   ├── provider.py           # دریافت دیتا از TSETMC
│   └── scheduler.py          # زمان‌بندی جمع‌آوری
├── database/
│   ├── __init__.py
│   ├── db.py                 # مدیریت دیتابیس SQLite
│   └── models.py             # ساختار جداول
├── dashboard/
│   └── app.py                # داشبورد Streamlit
├── tests/
│   ├── __init__.py
│   └── test_provider.py      # تست‌های خودکار
├── data/                     # دیتابیس و فایل‌ها (gitignored)
├── logs/                     # لاگ‌ها (gitignored)
├── requirements.txt
├── README.md
└── run_collector.py          # اسکریپت اصلی اجرا
```

## 🚀 شروع سریع

### ۱. فعال‌سازی محیط
```powershell
.\venv\Scripts\Activate.ps1
```

### ۲. اجرای تست اولیه (۵ نماد)
```powershell
python run_collector.py
```

### ۳. اجرای داشبورد
```powershell
streamlit run dashboard/app.py
```

## ⚙️ تنظیمات
فایل `config/settings.py` شامل تنظیمات اصلی مثل فهرست نمادها، بازه زمانی و محدودیت‌های درخواست است.

## 📊 فاز فعلی
- [x] ساختار پروژه
- [x] دریافت تاریخچه قیمت
- [x] ذخیره در SQLite
- [x] داشبورد اولیه
- [ ] دریافت داده لحظه‌ای (Snapshot)
- [ ] تحلیل تکنیکال
- [ ] مدل‌های AI
