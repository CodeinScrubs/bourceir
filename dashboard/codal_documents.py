"""نمایش ساده آرشیو اسناد عمومی و نسخه‌های مستقل آن‌ها."""

from pathlib import Path

import streamlit as st

from core.codal_documents import public_document_url
from core.timeutils import to_jalali
from ops.codal_documents import load_codal_document, recent_codal_documents


def document_archive_panel(job_panel):
    with st.expander('آرشیو متن و پیوست رسمی کدال'):
        st.caption('پیوند عمومی گزارش یا پیوست را وارد کن. هر دریافت نسخه مستقلی دارد. متن HTML ممکن است '
                   'جدول‌های پویا را نداشته باشد؛ اعداد، هویت شرکت و زمان انتشار تاریخی هنوز نیاز به بررسی دارند. '
                   'اطلاعات امروز به تاریخ گذشته منتقل نمی‌شوند.')
        url = st.text_input('پیوند سند عمومی کدال', key='codal_document_url')
        if url:
            try:
                url = public_document_url(url)
                job_panel('codal-document', ['codal-document', '--url', url], 'دریافت و حفظ نسخه سند',
                          'فقط سند عمومی؛ کپچا نیاز به تأیید خودت دارد. اسکریپت صفحه اجرا نمی‌شود.')
            except ValueError as exc:
                st.warning(str(exc))
        documents = recent_codal_documents()
        if not documents:
            st.info('هنوز سندی از این مسیر محفوظ نشده است.')
            return
        for row in documents:
            if row['status'] != 'captured':
                st.warning(row['message'])
                continue
            folder = Path(row['folder'])
            st.write(f"زمان حفظ نسخه: {to_jalali(row['archived_at'], with_time=True)} — {row['format'].upper()}")
            st.code(row['source_url'], language=None, wrap_lines=True)
            # قبل از تحویل بایت فایل نیز اثر انگشت کنترل می‌شود.
            checked = load_codal_document(folder)
            st.download_button('دریافت فایل محفوظ', (folder / checked['content_file']).read_bytes(),
                               file_name=checked['content_file'], key=checked['snapshot_id'])
            if checked['links']:
                st.caption('پیوندهای عمومی این نسخه؛ فایل پیوندشده یک دریافت مستقل می‌خواهد:')
                for index, link in enumerate(checked['links'], start=1):
                    st.link_button(f'باز کردن پیوند عمومی {index}', link)
