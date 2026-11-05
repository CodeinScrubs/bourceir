"""Navigation, capability discovery and local report access; temporary data only."""

import argparse

import pytest
from streamlit.testing.v1 import AppTest

from brs import build_parser
from config import settings
from dashboard.navigation import BY_LABEL, BY_SLUG, PAGES, search_pages
from ops.dashboard_catalog import (
    COMMAND_PAGES,
    MAX_FILE_BYTES,
    capability_catalog,
    contained,
    document_catalog,
    filter_tools,
    help_command,
    list_library,
    read_library_file,
    report_preview,
)


def test_every_cli_command_and_route_is_reachable():
    tools = capability_catalog(settings.BASE_DIR)
    commands = next(a for a in build_parser()._actions if isinstance(a, argparse._SubParsersAction)).choices
    assert {tool.name for tool in tools if tool.group == 'فرمان اصلی'} == set(commands) == set(COMMAND_PAGES)
    assert all(tool.page in BY_SLUG for tool in tools)
    assert len({tool.key for tool in tools}) == len(tools)
    assert len(BY_SLUG) == len(BY_LABEL) == len(PAGES)
    assert any(tool.name == 'ops.rights_daily_retrospective' for tool in tools)
    assert any(tool.name == 'research.surge_portfolio_run' for tool in tools)


def test_module_catalog_never_imports_discovered_source(tmp_path):
    folder = tmp_path/'ops'
    folder.mkdir()
    (folder/'new_tool.py').write_text('"""ابزار تخصصی آزمون"""\nraise RuntimeError("MUST NOT RUN")\n'
                                    'if __name__ == "__main__":\n    main()\n', encoding='utf-8')
    (folder/'ordinary.py').write_text('"""not an entry point"""\n', encoding='utf-8')
    (folder/'broken.py').write_text('this is not valid python!', encoding='utf-8')
    tools = capability_catalog(tmp_path)
    assert [tool.name for tool in tools if tool.group == 'ابزار تخصصی'] == ['ops.new_tool']
    tool = next(tool for tool in tools if tool.name == 'ops.new_tool')
    assert tool.description == 'ابزار تخصصی آزمون'
    assert help_command(tool).endswith('-m ops.new_tool --help')
    assert filter_tools(tools, 'آزمون', 'ابزار تخصصی') == [tool]


@pytest.mark.parametrize('query,name', [
    ('حق تقدم', 'ops.rights_daily_retrospective'),
    ('افزايش سرمايه', 'ops.capital_actions'),
    ('صف خرید', 'ops.queue_fall_archive'),
    ('backtest', 'research.anniversary_run'),
    ('بک تست', 'research.anniversary_run'),
    ('کدال', 'ops.codal_search'),
])
def test_advanced_tools_are_discoverable_from_persian_and_english_search(query, name):
    tools = capability_catalog(settings.BASE_DIR)
    assert name in {tool.name for tool in filter_tools(tools, query)}
    assert not filter_tools(tools, query, 'نوع ناموجود')


def test_library_is_paged_and_does_not_read_source_or_hidden_files(tmp_path):
    for i in range(125):
        (tmp_path/f'report-{i:03d}.json').write_text('{}', encoding='utf-8')
    (tmp_path/'source.py').write_text('raise Exception()', encoding='utf-8')
    (tmp_path/'.private.json').write_text('{}', encoding='utf-8')
    (tmp_path/'nested').mkdir()
    result = list_library(tmp_path)
    assert result['total'] == 126 and len(result['entries']) == 100
    assert result['entries'][0]['directory']
    second = list_library(tmp_path, offset=100)
    assert len(second['entries']) == 26
    assert not ({x['relative'] for x in result['entries']} & {x['relative'] for x in second['entries']})
    assert list_library(tmp_path, query='report-124')['total'] == 1
    assert list_library(tmp_path, 'nested')['total'] == 0


def test_library_search_finds_persian_names_for_technical_folders(tmp_path):
    (tmp_path/'rights-history').mkdir()
    (tmp_path/'market-reviews').mkdir()
    found = list_library(tmp_path, query='حق تقدم')
    assert found['total'] == 1
    assert found['entries'][0]['relative'] == 'rights-history'
    assert 'حق تقدم' in found['entries'][0]['label']


@pytest.mark.parametrize('relative', [
    '../secrets.json', '/outside.json', 'C:/outside.json', 'folder/../secrets.json',
    'C:outside.json', r'\\server\share\outside.json', r'folder\..\outside.json', r'\outside.json',
])
def test_library_rejects_escape_paths(tmp_path, relative):
    with pytest.raises(ValueError):
        contained(tmp_path, relative)


def test_library_rejects_external_symlinks(tmp_path):
    root = tmp_path/'library'
    root.mkdir()
    outside = tmp_path/'outside.json'
    outside.write_text('{"private":true}', encoding='utf-8')
    try:
        (root/'link.json').symlink_to(outside)
    except OSError:
        pytest.skip('Windows symlink privilege is unavailable')
    assert list_library(root)['total'] == 0
    with pytest.raises(ValueError):
        read_library_file(root, 'link.json')


def test_large_or_executable_files_cannot_be_previewed(tmp_path):
    with (tmp_path/'large.json').open('wb') as stream:
        stream.truncate(MAX_FILE_BYTES+1)
    (tmp_path/'run.py').write_text('raise Exception()', encoding='utf-8')
    for name in ('large.json', 'run.py'):
        with pytest.raises(ValueError):
            read_library_file(tmp_path, name)
    (tmp_path/'report.html').write_text('<script>window.MUST_NOT_RUN=1</script>', encoding='utf-8')
    name, body = read_library_file(tmp_path, 'report.html')
    assert report_preview(name, body) == ('download', None)
    assert report_preview('report.json', b'{"unknown":null}') == ('json', {'unknown': None})
    assert report_preview('report.json', b'{"profit":1,"profit":2}')[0] == 'text'
    assert report_preview('report.json', b'{"profit":NaN}')[0] == 'text'
    assert report_preview('report.json', b'{"profit":1e999}')[0] == 'text'


def test_document_catalog_handles_nested_guides(tmp_path):
    (tmp_path/'docs/research').mkdir(parents=True)
    (tmp_path/'docs/research/rights.md').write_text('# راهنمای حق تقدم\nمتن', encoding='utf-8')
    found = document_catalog(tmp_path)
    assert found == [dict(relative='docs/research/rights.md', title='راهنمای حق تقدم')]


def navigation_app():
    return AppTest.from_string('''
import streamlit as st
from dashboard.navigation import PAGES, launchpad, sidebar_navigation
with st.sidebar:
    chosen=sidebar_navigation({page.label: None for page in PAGES})
st.title(chosen)
launchpad()
''')


def test_navigation_buttons_and_direct_links_stay_in_sync():
    app = navigation_app().run()
    next(button for button in app.button if button.key == 'hub-open-paper').click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == BY_SLUG['paper'].label
    assert app.query_params['page'] == 'paper'
    app.sidebar.radio[0].set_value(BY_SLUG['reports'].label).run()
    assert app.query_params['page'] == 'reports'
    app.query_params['page'] = 'alerts'
    app.run()
    assert app.sidebar.radio[0].value == BY_SLUG['alerts'].label
    app.query_params['page'] = ['paper', 'broker']
    app.run()
    assert not app.exception
    assert app.sidebar.radio[0].value == BY_SLUG['home'].label
    assert any('نشانی این بخش' in value.value for value in app.warning)


def test_search_understands_persian_aliases_and_all_pages_are_in_launchpad():
    assert BY_SLUG['alerts'] in search_pages('صف فروش')
    assert BY_SLUG['broker'] in search_pages('كارگزاري')
    app = navigation_app().run()
    app.radio(key='brs-launchpad-mode').set_value('همه بخش‌ها').run()
    assert {button.key.removeprefix('hub-open-') for button in app.button
            if button.key and button.key.startswith('hub-open-')} == set(BY_SLUG)-{'home'}


def test_html_report_is_download_only_in_the_library(tmp_path, monkeypatch):
    from dashboard.hub import _tools

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    root = tmp_path/'private'
    root.mkdir()
    (root/'report.html').write_text('<script>window.MUST_NOT_RUN=1</script>', encoding='utf-8')
    _tools.clear()
    app = AppTest.from_string('from dashboard.hub import page_reports\npage_reports()').run()
    selected = next(select for select in app.selectbox if select.label == 'پوشه یا گزارش')
    selected.set_value('report.html').run()
    next(button for button in app.button if button.key == 'hub-library-read').click().run()
    assert not app.exception
    assert any('این قالب فقط' in item.value for item in app.info)
    assert not any('MUST_NOT_RUN' in item.value for item in app.markdown)


def test_tools_page_is_read_only_and_exposes_advanced_access(tmp_path, monkeypatch):
    from ops import jobs

    def forbidden(*_, **__):
        raise AssertionError('Rendering the catalog must not launch a job')

    monkeypatch.setattr(jobs, 'start_job', forbidden)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    app = AppTest.from_string('from dashboard.hub import page_tools\npage_tools()').run()
    assert not app.exception
    app.selectbox(key='hub-tool-group').set_value('پژوهش پیشرفته').run()
    assert not app.exception
    assert any('--help' in item.value and '-m research.' in item.value for item in app.code)
