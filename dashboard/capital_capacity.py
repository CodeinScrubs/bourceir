"""نمای محفوظ هزینه و ظرفیت؛ انتخاب اندازه سودده یا صدور سیگنال نمی‌کند."""

import json

import pandas as pd
import streamlit as st

from config import settings
from core.timeutils import to_jalali
from ops.capital_capacity import read_capacity_study


def page_capital_capacity():
    st.title('💸 هزینه و ظرفیت سرمایه')
    st.caption('همان انتخاب‌ها و قواعد محفوظ در اندازه‌های مختلف سرمایه؛ هزینه، نقد بیکار و نامعلوم کنار بازده دیده می‌شوند.')
    root = settings.DATA_DIR / 'private' / 'capital-capacity'
    studies = sorted(root.glob('CAPACITY-*'), key=lambda p: p.stat().st_mtime, reverse=True) if root.exists() else []
    if not studies:
        st.info('هنوز مطالعه‌ای ثبت نشده است. نتیجه یا قیمت نمونه ساخته نمی‌شود.')
        return
    selected = st.selectbox('مطالعه محفوظ', studies, format_func=lambda p: p.name)
    if not (selected / 'result.json').exists():
        st.warning('این تلاش هنوز نتیجه کامل ندارد؛ ممکن است در حال اجرا، متوقف یا ناموفق باشد. از فهرست حذف نشده است.')
        return
    try:
        result = read_capacity_study(selected)
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        st.error('گزارش قابل تأیید نیست: ' + str(exc))
        return
    st.write('زمان حفظ نتیجه: ' + to_jalali(result['created_at'], with_time=True))
    st.write(f"{len(result['cases']):,} بازاجرای برش | {len(result['plan']['scenarios'])} اندازه سرمایه | "
             f"{len(result['plan']['modes'])} حالت شامل تمام کنترل‌ها")
    st.warning('سودآوری هنوز تأیید نشده است. سرمایه آغاز هر برش، نقد فرضی است؛ موجودی و نقد واقعی سبد فعلی بازسازی نشده‌اند.')
    show_controls = st.checkbox('نمایش همه کنترل‌ها', value=False)
    rows = []
    for scenario, modes in result['summary'].items():
        for mode, row in modes.items():
            if not show_controls and '-random' in mode:
                continue
            rows.append({'اندازه': 'سرمایه مرحله مالک' if scenario == 'owner' else scenario.replace('x', 'ضریب '),
                'روش محفوظ': mode, 'برش معلوم/کل': f"{row['known_folds']}/{row['folds']}",
                'برش معلوم با خرید': row['invested_known_folds'], 'برش معلوم بدون خرید': row['idle_known_folds'],
                'میانگین بازده برش معلوم (%)': None if row['mean_known_fold_return'] is None else row['mean_known_fold_return'] * 100,
                'میانگین کارمزد / سرمایه اولیه (%)': row['mean_recorded_fee_fraction'] * 100,
                'میانگین سهم غیرنقد برش معلوم (%)': None if row['mean_known_noncash_equity_share'] is None else row['mean_known_noncash_equity_share'] * 100,
                'بیشترین افت برش معلوم (%)': None if row['maximum_known_drawdown'] is None else row['maximum_known_drawdown'] * 100,
                'ورود شبیه‌سازی‌شده / نامزد (%)': None if row['pooled_model_entry_fill_fraction'] is None else row['pooled_model_entry_fill_fraction'] * 100,
                'برش با سود مثبت': row['positive_known_folds'],
                'سود ناکافی حتی با بازگردانی کارمزد': row['diagnoses'].get('nonpositive_even_after_recorded_fee_addback', 0),
                'زیان حساس به کارمزد همان اجراها': row['diagnoses'].get('fee_sensitive_fixed_fills', 0),
                'عبور اقدام شرکتی مصنوعی': row['corporate_action_events']})
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.info('میانگین حساب‌های مستقل، بازده سالانه یک حساب نیست. کاهش زیان همراه با معامله کمتر یا نقد بیشتر، مزیت انتخاب سهم را ثابت نمی‌کند.')
    st.caption('هش گزارش، برنامه و جمع تمام برش‌ها بررسی شد. کنترل کامل فایل‌ها و بازحساب دفتر نقد با capital-review انجام می‌شود؛ '
               'این صفحه موتور را دوباره اجرا نمی‌کند.')
    st.caption('افزودن کارمزد ثبت‌شده به سود همان اجراها، بکتست بدون هزینه نیست: اندازه سفارش، صف، نقد، توقف ورود و ترکیب معاملات عوض نشده‌اند. '
               'آمار مالی حقوق شرکتی و امکان پرشدن واقعی سفارش همچنان تأیید کامل ندارند.')
