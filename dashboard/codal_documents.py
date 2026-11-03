"""نمایش ساده آرشیو اسناد عمومی و نسخه‌های مستقل آن‌ها."""

import sqlite3
from pathlib import Path

import streamlit as st

from config import settings
from core.codal_documents import public_document_url
from core.timeutils import to_jalali
from ops.codal_compact import catalog_path, search_catalog
from ops.codal_documents import load_codal_document, read_codal_document_file, recent_codal_documents


def document_archive_panel(job_panel):
    archive_root = settings.DATA_DIR/'private/codal-documents'
    if catalog_path(archive_root).is_file():
        with st.expander('جست‌وجوی خلاصه اطلاعیه‌ها'):
            st.caption('خلاصه بر پایه متن قابل مشاهده است؛ تاریخ انتشار، هویت ناشر و ارقام باید با منبع بررسی شوند.')
            query = st.text_input('نماد، عنوان یا متن', key='codal_compact_query')
            if query.strip():
                try:
                    hits = search_catalog(archive_root, query, limit=20)
                    if not hits:
                        st.info('موردی در خلاصه‌های فعلی پیدا نشد.')
                    for hit in hits:
                        item = hit['summary']
                        st.write(f"{item['symbol'] or 'نماد نامعلوم'} — {item['title'] or 'عنوان نامعلوم'}")
                        st.caption(f"انتشار اظهارشده: {item['claimed_published_at'] or 'نامعلوم'} | "
                                   f"زمان مشاهده: {item['observed_at'] or 'نامعلوم'}")
                        if item['superseding_tracing_numbers']:
                            st.warning('این متن به نسخه جدیدتر اشاره می‌کند؛ پیش از نتیجه‌گیری نسخه جدید را بررسی کن.')
                        if item['document_format'] == 'pdf':
                            st.info('محتوای PDF در این خلاصه استخراج نشده و به بازبینی نیاز دارد.')
                        elif item['excerpts']:
                            st.text('\n'.join(item['excerpts']))
                        st.link_button('اطلاعیه منبع', item['source_url'])
                except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
                    st.warning(f'خلاصه قابل خواندن نیست: {exc}')
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
            st.download_button('دریافت فایل محفوظ', read_codal_document_file(folder, checked['content_file']),
                               file_name=checked['content_file'], key=checked['snapshot_id'])
            if checked['links']:
                st.caption('پیوندهای عمومی این نسخه؛ فایل پیوندشده یک دریافت مستقل می‌خواهد:')
                for index, link in enumerate(checked['links'], start=1):
                    st.link_button(f'باز کردن پیوند عمومی {index}', link)
