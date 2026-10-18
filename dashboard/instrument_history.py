"""نمای شواهد مشخصات؛ برچسب تاریخ درخواست با تاریخ تأییدشده اشتباه نمی‌شود."""

import pandas as pd
import streamlit as st

from config import settings
from core.timeutils import to_jalali
from ops.instrument_history import load_instrument_history


def show_instrument_history():
    root = settings.DATA_DIR / 'private' / 'instrument-history'
    folders = sorted(root.glob('HISTORY-*'), key=lambda p: p.stat().st_mtime_ns, reverse=True)[:10] if root.exists() else []
    if not folders:
        st.info('هنوز شاهد مشخصات تاریخی دریافت نشده است؛ حجم مبنای امروز به گذشته نسبت داده نمی‌شود.')
        return
    folder = st.selectbox('آرشیو مشخصات تاریخی', folders, format_func=lambda p: p.name)
    if not (folder / 'manifest.json').exists():
        st.warning('این تلاش هنوز آرشیو کامل ندارد؛ نتیجه ناقص از فهرست حذف نشده است.')
        return
    try:
        result = load_instrument_history(folder)
    except (ValueError, KeyError, TypeError, OSError) as error:
        st.error('آرشیو قابل تأیید نیست: ' + str(error))
        return
    st.warning('این داده هنوز مجاز به مصرف در فیچر یا بکتست نیست. تاریخ در نشانی درخواست، زمان انتشار تاریخی را ثابت نمی‌کند.')
    labels = {'request_url_only': 'فقط نشانی درخواست', 'response_matches_request': 'تصریح برابر در پاسخ',
              'conflicting_response': 'تعارض پاسخ', 'unavailable': 'نامعلوم'}
    rows = []
    for case in result['cases']:
        request, assessed = case['request'], case['assessment']
        rows.append({'نماد هنگام درخواست': request['symbol'], 'تاریخ درخواست': to_jalali(request['requested_date']),
            'تعداد سهام مشاهده‌شده': assessed['shares_outstanding'], 'حجم مبنای مشاهده‌شده': assessed['base_volume'],
            'شاهد تاریخ': labels[assessed['date_attestation']], 'شاهد هویت': labels[assessed['identity_attestation']],
            'دو فیلد عددی معلوم': 'بله' if assessed['candidate_fields_complete'] else 'خیر'})
    st.dataframe(pd.DataFrame(rows), hide_index=True)
    st.caption('هش برنامه، کد و همه پاسخ‌ها و بازحساب ارزیابی کنترل شدند؛ اصالت بیرونی، جامعه تاریخی و حقوق شرکتی تأیید نشده‌اند.')
