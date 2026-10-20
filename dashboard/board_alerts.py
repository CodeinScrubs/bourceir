"""وضعیت پایش مستقل فارابی در داشبورد؛ تنظیم و صدا در پنل سریع مستقل است."""
import pandas as pd
import streamlit as st

from config import settings
from core.timeutils import to_jalali
from ops.board_status import read_board_status


def board_alert_panel():
    st.subheader('هشدار سریع تابلو فارابی')

    @st.fragment(run_every=1)
    def panel():
        result = read_board_status(settings.BASE_DIR / 'data' / 'private')
        if result['state'] == 'absent':
            st.info('برای هشدار نیم‌ثانیه‌ای، WATCH_FARABI.bat را اجرا کنید و در مرورگر موقت وارد فارابی شوید.')
            return
        if result['state'] == 'invalid':
            st.error('وضعیت پایش معتبر نیست: ' + result['reason'])
            return
        if result['state'] != 'current':
            st.warning('پایش سریع متوقف یا کهنه است؛ اعداد زیر زنده نیستند.')
        st.link_button('بازکردن پنل سریع، تنظیم شرایط و فعال‌کردن صدا', result['url'])
        status = result['status']
        st.caption('این هشدارها برای بررسی شخصی تابلو هستند. ساعت مستقل صف و تأخیر بورس تا فارابی نامعلوم‌اند؛ '
                   'افت صف و افزایش حجم، برگشت قیمت یا اجرای سفارش را ثابت نمی‌کنند.')
        if status['boards']:
            st.dataframe(pd.DataFrame([{'نماد': q.get('symbol', q.get('key')), 'آخرین قیمت (ریال)': q.get('last_price'),
                'حجم روز': q.get('volume'), 'عرضه در کف': q.get('sell_queue'), 'تقاضا در سقف': q.get('buy_queue'),
                'وضعیت': 'مشاهده DOM' if q.get('ok') else q.get('error', 'نامعلوم')} for q in status['boards']]),
                hide_index=True, width='stretch')
        else:
            st.info('منتظر ورود یا بازکردن تابلوی معتبر در پنجره پایش هستیم.')
            if status.get('last_error'):
                st.warning(status['last_error']['reason'])
        labels = {'sell_queue_drop': 'افت صف فروش', 'sell_queue_drop_with_volume': 'افت صف فروش همراه حجم',
                  'buy_queue_drop': 'افت صف خرید', 'buy_queue_drop_with_volume': 'افت صف خرید همراه حجم',
                  'volume_burst': 'افزایش سریع حجم', 'price_above': 'عبور قیمت از بالا', 'price_below': 'عبور قیمت از پایین'}
        for event in reversed(status['alerts'][-5:]):
            st.write(f"{event.get('symbol', '')} — {labels.get(event.get('kind'), 'رویداد تابلو')} — "
                     f"{to_jalali(event.get('observed_at'), with_time=True)}")
    panel()
