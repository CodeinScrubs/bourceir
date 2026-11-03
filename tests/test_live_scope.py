"""A requested monitoring scope is distinct from stored rows and actual capture."""

import ast
from types import SimpleNamespace

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from config import settings
from core.text import to_fa_digits
from core.timeutils import now_tehran_iso, to_jalali
from dashboard.live_scope import requested_live_scope, requested_scope_label
from data_collector.monitor import DataFreshnessMonitor
from tests.test_reliability_076 import live_snapshot


def test_requested_symbols_are_separate_from_capture_and_normalized():
    scope = requested_live_scope(dict(running=True, args=['live', '--quote-source=dated-quote-context',
        '--symbols', 'كيان,فولاد,کیان', '--interval', '60']))
    assert scope == dict(state='explicit', symbols=['کیان', 'فولاد'], source='dated-quote-context', interval=60)
    assert requested_scope_label(scope, 'كيان') == 'نام در درخواست'
    assert requested_scope_label(scope, 'دیگر') == 'نام خارج از درخواست'


def test_stopped_and_default_do_not_assert_membership():
    stopped = requested_live_scope(dict(running=False, args=['live', '--symbols', 'کیان']))
    default = requested_live_scope(dict(running=True, args=['live']))
    assert requested_scope_label(stopped, 'کیان') == 'پایش متوقف'
    assert requested_scope_label(default, 'کیان') == 'نامعلوم از تنظیمات'
    assert stopped['symbols'] is default['symbols'] is None


@pytest.mark.parametrize('worker', [None, {}, {'running': None}, {'running': 'true'}])
def test_unknown_liveness_is_not_reported_as_stopped(worker):
    assert requested_live_scope(worker)['state'] == 'unknown'


@pytest.mark.parametrize('args', [None, [], ['update'], ['live', '--symbols'],
    ['live', '--symbols', 'الف', '--symbols=ب'], ['live', '--interval', '0'],
    ['live', '--interval', '9' * 5000], ['live', '--quote-source', 'unknown'],
    ['live', '--quote-source', 'dated-quote'], ['live', '--symbols', None],
    ['live', '--quote-source', 'dated-quote', '--symbols', ','.join(str(i) for i in range(11))]])
def test_malformed_job_state_does_not_create_a_scope(args):
    scope = requested_live_scope(dict(running=True, args=args))
    assert scope['state'] == 'unknown'
    assert requested_scope_label(scope, 'الف') == 'نامعلوم از تنظیمات'


def test_live_table_labels_old_rows_outside_request_and_updates_on_stop(monkeypatch, isolated_board_monitor):
    stamp = now_tehran_iso()
    row = live_snapshot(value=100_000., ref_price=100., close_price=100., captured_at=stamp,
        provider_timestamp=stamp, buy_queue_volume=0., sell_queue_volume=0.)
    source = pd.DataFrame([dict(row, symbol_fa='کیان'), dict(row, symbol_fa='دیگر')])
    worker = dict(running=True, args=['live', '--symbols', 'كيان', '--quote-source', 'dated-quote-context'])
    tree = ast.parse((settings.BASE_DIR / 'dashboard/app.py').read_text(encoding='utf-8'))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == 'page_live')
    namespace = dict(st=st, pd=pd, jobs=SimpleNamespace(job_status=lambda _: worker),
        latest_snapshots=lambda: source, DataFreshnessMonitor=DataFreshnessMonitor,
        to_jalali=to_jalali, to_fa_digits=to_fa_digits, interval_panel=lambda _: None)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'live_scope_test', 'exec'), namespace)
    monkeypatch.setattr(st, '_brs_live_scope_test', namespace['page_live'], raising=False)
    app = AppTest.from_string('import streamlit as st\nst._brs_live_scope_test()').run()
    assert not app.exception
    table = next(frame.value for frame in app.dataframe if 'نام در درخواست پایش' in frame.value.columns).set_index('نماد')
    assert table.loc['کیان', 'نام در درخواست پایش'] == 'نام در درخواست'
    assert table.loc['دیگر', 'نام در درخواست پایش'] == 'نام خارج از درخواست'
    assert any('دریافت موفق' in item.value for item in app.caption)
    worker['running'] = False
    app.run()
    assert not app.exception
    table = next(frame.value for frame in app.dataframe if 'نام در درخواست پایش' in frame.value.columns)
    assert set(table['نام در درخواست پایش']) == {'پایش متوقف'}
    assert any('پایش زنده خاموش است' in item.value for item in app.warning)
    worker.pop('running')
    app.run()
    assert not app.exception
    table = next(frame.value for frame in app.dataframe if 'نام در درخواست پایش' in frame.value.columns)
    assert set(table['نام در درخواست پایش']) == {'نامعلوم از تنظیمات'}
    assert any('وضعیت فرایند یا دامنه درخواست' in item.value for item in app.warning)
