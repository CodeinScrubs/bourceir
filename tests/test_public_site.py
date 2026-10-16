"""شبکه ساختگی فقط در آزمون؛ صفحات و کد ضمیمه هرگز اجرا نمی‌شوند."""

import json

import pytest

from data_collector.public_site import PublicPage, PublicSiteArchive, public_url


class Response:
    def __init__(self,body='',status=200,content_type='text/html',location=None):
        self.body=body.encode()
        self.status_code=status
        self.headers={'Content-Type':content_type}
        if location:self.headers['Location']=location
    def iter_content(self,_):yield self.body
    def close(self):pass


class Session:
    def __init__(self,routes):self.routes,self.calls=routes,[]
    def get(self,url,**_):
        self.calls.append(url)
        return self.routes.get(url,Response(status=404))


def session():
    return Session({
        'https://example.test/robots.txt':Response('User-agent: *\nDisallow: /blocked\nSitemap: https://example.test/sitemap.xml',200,'text/plain'),
        'https://example.test/sitemap.xml':Response('<urlset><url><loc>https://example.test/dir/page</loc></url><url><loc>https://example.test/blocked</loc></url></urlset>',content_type='application/xml'),
        'https://example.test/':Response('<title>نمونه</title><a href="/login">login</a>'),
        'https://example.test/dir/page':Response('<a href="child">فرزند</a><script src="/app.js"></script>'),
        'https://example.test/dir/child':Response('<p>عمومی</p>'),
        'https://example.test/app.js':Response('throw new Error("never execute");const origin="https://data.example.test";',content_type='text/javascript'),
    })


def test_relative_links_scope_auth_query_and_no_execution(tmp_path,monkeypatch):
    monkeypatch.setattr('data_collector.public_site.time.sleep',lambda _:None)
    fake=session()
    crawler=PublicSiteArchive('https://example.test/',tmp_path,session=fake)
    result=crawler.crawl()
    assert result['sitemap_urls']==2 and result['sitemap_successful']==1
    assert result['skipped_by_robots']==['https://example.test/blocked']
    assert 'https://example.test/login' not in fake.calls
    assert 'https://example.test/dir/child' in fake.calls
    assert not result['backend_access_verified'] and not result['javascript_execution_verified']
    script=[r for r in crawler.records if r['category']=='script'][0]
    assert script['referenced_origins']==['data.example.test']
    assert public_url('https://other.test/x','https://example.test/') is None
    assert public_url('/login','https://example.test/') is None
    assert public_url('/%61pi/orders','https://example.test/') is None
    assert public_url('/public?token=x','https://example.test/') is None
    assert public_url('child','https://example.test/dir/page')=='https://example.test/dir/child'
    assert public_url('/نماد','https://example.test/')==public_url('/%D9%86%D9%85%D8%A7%D8%AF','https://example.test/')
    assert public_url('/%25D9%2586','https://example.test/') is None


def test_resume_verifies_source_hash_and_does_not_refetch_pages(tmp_path,monkeypatch):
    monkeypatch.setattr('data_collector.public_site.time.sleep',lambda _:None)
    first=PublicSiteArchive('https://example.test/',tmp_path,session=session())
    first.crawl()
    fake=session()
    PublicSiteArchive('https://example.test/',tmp_path,session=fake).crawl()
    assert fake.calls==['https://example.test/robots.txt','https://example.test/sitemap.xml']
    path=tmp_path/first.records[2]['body_file']
    path.write_bytes(b'corrupted')
    with pytest.raises((ValueError,OSError)):
        PublicSiteArchive('https://example.test/',tmp_path,session=session())


def test_rate_limit_stops_whole_crawl_and_private_paths_not_followed(tmp_path,monkeypatch):
    monkeypatch.setattr('data_collector.public_site.time.sleep',lambda _:None)
    fake=session()
    fake.routes['https://example.test/']=Response(status=429)
    result=PublicSiteArchive('https://example.test/',tmp_path,session=fake).crawl()
    assert result['stopped']=='HTTP 429'
    assert 'https://example.test/dir/page' not in fake.calls


def test_parser_ignores_inline_scripts_and_budget_reports_incomplete(tmp_path,monkeypatch):
    monkeypatch.setattr('data_collector.public_site.time.sleep',lambda _:None)
    parsed=PublicPage()
    parsed.feed('<p>داده</p><script>alert("execute")</script><style>secret</style>')
    assert parsed.parts==['داده']
    result=PublicSiteArchive('https://example.test/',tmp_path,session=session(),max_pages=1).crawl()
    assert result['stopped']=='page_budget' and result['sitemap_unresolved']
    coverage=json.loads((tmp_path/'coverage.json').read_text(encoding='utf-8'))
    assert coverage==result


def test_manifest_rejects_path_escape_before_read(tmp_path):
    (tmp_path/'sources.jsonl').write_text(json.dumps(dict(url='https://example.test/',body_file='../secret',sha256='x'))+'\n',encoding='utf-8')
    with pytest.raises(ValueError):PublicSiteArchive('https://example.test/',tmp_path,session=session())


def test_historical_trade_provider_preserves_raw_and_validates_request():
    from data_collector.tsetmc import TsetmcProvider
    from tests.conftest import FakeSession
    provider=TsetmcProvider(session=FakeSession({'/GetTradeHistory/123/20240203/false':{'tradeHistory':[]}}))
    observed=provider.get_trade_session('123','2024-02-03')
    assert observed['response']=={'tradeHistory':[]} and observed['completeness_verified'] is False
    assert observed['endpoint_is_grouped'] is False
    with pytest.raises(ValueError):provider.get_trade_session('../account','2024-02-03')
    with pytest.raises(ValueError):provider.get_trade_session('123','20240203')
    assert provider.get_trade_session('456','2024-02-03') is None
