"""فهرست‌سازی ثابتِ متن ناشناس؛ اجرای کد ضمیمه ممنوع است."""

from ops.source_catalog import catalog_claim_files, text_catalog


def test_static_catalog_preserves_duplicate_numbers_and_source_lines(tmp_path):
    text='فیلتر شماره ۱: نمونه\n[ih].QTotTran5J\nفیلتر شماره ۱: نسخه دیگر\nif(trend=1) { }\n'
    result=text_catalog(text)
    assert [r['number'] for r in result['sections']]==[1,1]
    assert result['sections'][0]['start_line']==1 and result['sections'][0]['end_line']==2
    assert [r['line'] for r in result['findings']]==[2,4]
    assert not result['untrusted_code_executed']
    source=tmp_path/'source.txt'
    source.write_text(text,encoding='utf-8')
    catalog=catalog_claim_files([source],tmp_path/'catalog')
    assert catalog['indexed_lines']==4 and catalog['source_count']==1
    assert (tmp_path/'catalog'/(catalog['sources'][0]['sha256']+'.txt.gz')).exists()


def test_comparisons_are_not_assignment_and_text_is_not_executed():
    text='if(x==1) {}\nif(x<=1) {}\neval("raise exception")\n'
    findings=text_catalog(text)['findings']
    assert [r['kind'] for r in findings]==['external_execution_review']


def test_site_catalog_rebuilds_text_from_verified_body(tmp_path,monkeypatch):
    import gzip

    from data_collector.public_site import PublicSiteArchive
    from ops.source_catalog import catalog_site
    from tests.test_public_site import session
    monkeypatch.setattr('data_collector.public_site.time.sleep',lambda _:None)
    archive=PublicSiteArchive('https://example.test/',tmp_path,session=session())
    archive.crawl()
    record=next(r for r in archive.records if r.get('title')=='نمونه')
    (tmp_path/record['text_file']).write_bytes(gzip.compress(b'corrupted optional extracted text'))
    result=catalog_site(tmp_path)
    page=next(r for r in result['pages'] if r['url']=='https://example.test/')
    assert page['visible_text_chars']==len('نمونه\nlogin')
    assert not result['private_backend_verified']
