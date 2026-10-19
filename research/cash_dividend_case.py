"""پرونده حسابداری با انتخاب و حقوق صریح؛ دو مدل ثابت، بدون جست‌وجو یا سیگنال."""

import argparse
import hashlib
import io
import json
import uuid
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from backtest.cash_dividends import CashDividend, replay_cash_dividend_portfolio
from backtest.portfolio import PortfolioConfig, replay_portfolio
from config import settings
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.dividend_reconciliation import load_dividend_comparison
from ops.report_files import read_private_report, write_private_report
from research.run import _json_safe, archive_research_code

VERSION = 'cash-dividend-accounting-case-v1'
MODES = ('legacy_synthetic', 'fixed_shares_unpaid_claim')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_prices(path, cutoff, *, allow_empty=False):
    with Path(path).open('rb') as handle:
        contents = handle.read(64 * 1024 * 1024 + 1)
    if len(contents) > 64 * 1024 * 1024:
        raise ValueError('جدول پرونده باید کمتر از ۶۴ مگابایت باشد')
    buffer = io.BytesIO(contents)
    if not pq.read_schema(buffer).names and allow_empty:
        buffer.seek(0)
        return pd.read_parquet(buffer)
    buffer.seek(0)
    dates = pd.read_parquet(buffer, columns=['date']).date.tolist()
    if (not dates and not allow_empty) or any(not is_canonical_iso_date(day) or day >= cutoff for day in dates):
        raise ValueError('قیمت پرونده باید فقط پیش از مرز مهروموم باشد')
    buffer.seek(0)
    return pd.read_parquet(buffer)


def _source_names():
    names = {'requirements.lock.txt', 'pyproject.toml'}
    for directory in ('core', 'features', 'database', 'config', 'research', 'backtest'):
        names.update(p.relative_to(settings.BASE_DIR).as_posix() for p in (settings.BASE_DIR / directory).rglob('*')
                     if p.is_file() and p.suffix in ('.py', '.yaml', '.sql'))
    return names


def _compare(prices, calendar, selection, event, config):
    return {
        'legacy_synthetic': replay_portfolio(prices, selection, calendar, config),
        'fixed_shares_unpaid_claim': replay_cash_dividend_portfolio(
            prices, selection, calendar, config, events=[event], payments=[]),
    }


def _summary(outputs):
    result = {}
    for name, replay in outputs.items():
        frame = replay.executions
        buys = frame.loc[frame.side == 'buy'] if not frame.empty else frame
        sells = frame.loc[frame.side == 'sell'] if not frame.empty else frame
        result[name] = dict(metrics=_json_safe(replay.metrics), assumptions=_json_safe(replay.assumptions),
            bought_shares=0 if buys.empty else float(buys.quantity.sum()),
            sold_shares=0 if sells.empty else float(sells.quantity.sum()),
            cash_final_rial=float(replay.equity.iloc[-1].cash_rial))
    return result


def run_cash_dividend_case(db, *, comparison, decision_date, record_date, ex_date, end_exclusive,
                           initial_capital_rial=None, horizon=1):
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    dates = (decision_date, record_date, ex_date, end_exclusive)
    if (not is_canonical_iso_date(cutoff) or any(not is_canonical_iso_date(d) for d in dates)
            or any(d >= cutoff for d in dates[:3]) or end_exclusive > cutoff
            or not decision_date < record_date < ex_date < end_exclusive):
        raise ValueError('ترتیب چهار تاریخ استاندارد و تماماً پیش از مرز لازم است')
    source = load_dividend_comparison(comparison)
    fact = source['facts']['primary']
    if not source['facts']['cross_source_value_corroborated']:
        raise ValueError('مبلغ تطبیق‌شده سند اولیه و منبع لازم است')
    if not record_date < fact['meeting_date'] < ex_date:
        raise ValueError('مجمع باید بین آخرین جلسه مشمول و جلسه بدون حقِ فرض مدل باشد')
    meta = db.get_symbol(fact['symbol'])
    if meta is None or meta['instrument_type_code'] not in (300, 303):
        raise ValueError('هویت فعلی سهام عادی لازم است؛ هویت تاریخی همچنان فرض است')
    columns = ['date', 'open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw',
               'ref_price_raw', 'volume', 'value', 'trade_count']
    own = not db.conn.in_transaction
    if own:
        db.conn.execute('BEGIN')
    try:
        prices = pd.read_sql_query('SELECT ' + ','.join(columns) + ' FROM daily_prices WHERE symbol_id=? '
            'AND source=? AND date>=? AND date<? ORDER BY date', db.conn,
            params=(meta['id'], 'tsetmc', decision_date, end_exclusive))
        calendar = [r[0] for r in db.conn.execute('SELECT DISTINCT p.date FROM daily_prices p JOIN symbols s '
            'ON s.id=p.symbol_id WHERE p.source=? AND s.instrument_type_code IN (300,303) AND p.date>=? AND p.date<? '
            'ORDER BY p.date', ('tsetmc', decision_date, end_exclusive))]
    finally:
        if own:
            db.conn.rollback()
    if prices.empty or decision_date not in set(prices.date) or any(d not in calendar for d in (record_date, ex_date)):
        raise ValueError('قیمت/تقویم مشاهده‌شده برای تاریخ‌های صریح کافی نیست')
    prices.insert(0, 'symbol', fact['symbol'])
    selection = pd.DataFrame([dict(date=decision_date, symbol=fact['symbol'], rank=0)])
    capital = settings.BACKTEST_CONFIG['initial_capital'] if initial_capital_rial is None else initial_capital_rial
    config = PortfolioConfig(initial_capital_rial=capital, horizon=horizon, max_positions=1,
        slippage_bps=settings.BACKTEST_CONFIG['default_slippage_bps'],
        participation=min(.05, settings.BACKTEST_CONFIG['max_volume_participation_pct']))
    event = CashDividend('ANNUAL-' + fact['symbol'].encode().hex() + '-' + fact['meeting_date'],
        fact['symbol'], record_date, ex_date, fact['dps_rial'], _sha(Path(comparison) / 'manifest.json'))
    identifier = f'EXP-{now_tehran_iso()[:10]}-CASHRIGHTS-{uuid.uuid4().hex}'
    folder = settings.DATA_DIR / 'private' / 'cash-dividend-cases' / identifier
    folder.mkdir(parents=True, exist_ok=False)
    prices.to_parquet(folder / 'prices.parquet', index=False)
    selection.to_parquet(folder / 'selection.parquet', index=False)
    write_private_report(folder / 'calendar.json', dict(dates=calendar))
    inputs = {name: _sha(folder / name) for name in ('prices.parquet', 'selection.parquet', 'calendar.json')}
    sources = archive_research_code(folder / 'code')
    protocol = dict(version=VERSION, experiment_id=identifier, registered_at=now_tehran_iso(),
        modes=list(MODES), config=asdict(config), event=asdict(event), payments=[],
        input_sha256=inputs, code_sha256=sources, cutoff=cutoff, comparison=str(Path(comparison).resolve()),
        dates=dict(decision=decision_date, record=record_date, ex=ex_date, end_exclusive=end_exclusive),
        comparison_manifest_sha256=_sha(Path(comparison) / 'manifest.json'),
        selection='one explicit decision before prior-selected source event; no ranking or result-based tuning',
        calendar_source='observed ordinary-equity price dates; not verified historical market calendar',
        historical_entitlement_mapping_verified=False, historical_identity_verified=False,
        settlement_verified=False, independent_strategy_test=False, accepted_for_signals=False,
        created_market_data=False, account_data_read=False)
    write_private_report(folder / 'protocol.json', protocol)
    encoded = json.dumps(protocol, ensure_ascii=False, allow_nan=False)
    common = dict(symbols_tested=fact['symbol'], start_date=decision_date, end_date=end_exclusive,
                  dataset_version=protocol['comparison_manifest_sha256'], features_version='raw_prices_only', model_version=VERSION)
    if db.log_experiment(experiment_id=identifier+'-PROTOCOL', strategy_name='cash-dividend-accounting-protocol',
        parameters_json=encoded, metrics_json='{}', **common) is None:
        raise RuntimeError('ثبت پروتکل پیش از حسابداری ناموفق بود')
    outputs = _compare(prices, calendar, selection, event, config)
    for name, replay in outputs.items():
        for label in ('equity', 'executions', 'decisions'):
            getattr(replay, label).to_parquet(folder / f'{name}-{label}.parquet', index=False)
    result = dict(version=VERSION, experiment_id=identifier, completed_at=now_tehran_iso(),
        modes=_summary(outputs), holdout_opened=False, accepted_for_signals=False, profitability_proven=False)
    # فایل‌های آرشیوشده LF دارند؛ مقایسه مستقل از خط پایان انجام می‌شود.
    current = {name: hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
               for name in sources}
    if current != sources or any(_sha(folder / name) != digest for name, digest in inputs.items()):
        raise RuntimeError('کد/ورودی هنگام حسابداری تغییر کرد')
    write_private_report(folder / 'result.json', result)
    if db.log_experiment(experiment_id=identifier, strategy_name='cash-dividend-accounting-case',
        parameters_json=encoded, metrics_json=json.dumps(result, ensure_ascii=False, allow_nan=False), **common) is None:
        raise RuntimeError('ثبت نتیجه حسابداری ناموفق بود')
    files = {p.name: _sha(p) for p in folder.iterdir() if p.is_file()}
    write_private_report(folder / 'manifest.json', dict(version=VERSION, files=files))
    return dict(folder=str(folder), **result)


def review_cash_dividend_case(folder):
    folder = Path(folder).resolve()
    manifest = read_private_report(folder / 'manifest.json')
    expected = {'protocol.json', 'result.json', 'prices.parquet', 'selection.parquet', 'calendar.json'}
    expected |= {f'{mode}-{label}.parquet' for mode in MODES for label in ('equity', 'executions', 'decisions')}
    if manifest['version'] != VERSION or set(manifest['files']) != expected:
        raise ValueError('نسخه یا مخرج فایل‌های پرونده برابر نیست')
    if any(_sha(folder / name) != digest for name, digest in manifest['files'].items()):
        raise ValueError('اثر انگشت پرونده حسابداری برابر نیست')
    protocol, result = [read_private_report(folder / name) for name in ('protocol.json', 'result.json')]
    if (protocol['version'] != VERSION or protocol['modes'] != list(MODES) or protocol['payments'] != []
            or not is_canonical_iso_date(protocol['cutoff'])
            or protocol['cutoff'] > settings.RESEARCH_CONFIG['holdout_start']
            or any(protocol.get(k) is not False for k in ('settlement_verified', 'historical_entitlement_mapping_verified',
                                                        'historical_identity_verified', 'independent_strategy_test', 'accepted_for_signals'))):
        raise ValueError('قرارداد یا مرزهای پرونده ارتقا یافته است')
    if (result['version'] != VERSION or result['experiment_id'] != protocol['experiment_id']
            or any(result.get(k) is not False for k in ('accepted_for_signals', 'profitability_proven', 'holdout_opened'))):
        raise ValueError('هویت یا پرچم نتیجه برابر نیست')
    if set(protocol['code_sha256']) != _source_names():
        raise ValueError('مخرج کد محفوظ پرونده کامل نیست')
    for name, digest in protocol['code_sha256'].items():
        archived = (folder / 'code' / name).resolve()
        if not archived.is_relative_to(folder / 'code') or _sha(archived) != digest:
            raise ValueError('کد محفوظ پرونده تغییر کرده است')
        if hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != digest:
            raise ValueError('بازاجرای پرونده به همان کد محفوظ نیاز دارد؛ نتیجه قدیمی ارتقا نمی‌یابد')
    source = load_dividend_comparison(protocol['comparison'])
    if (not source['facts']['cross_source_value_corroborated']
            or _sha(Path(protocol['comparison']) / 'manifest.json') != protocol['comparison_manifest_sha256']):
        raise ValueError('تطبیق اولیه مبلغ تغییر کرده است')
    event = CashDividend(**protocol['event'])
    fact, dates = source['facts']['primary'], protocol['dates']
    if (event.symbol != fact['symbol'] or Decimal(event.dps_rial) != Decimal(fact['dps_rial'])
            or event.evidence_sha256 != protocol['comparison_manifest_sha256']
            or event.record_date != dates['record'] or event.ex_date != dates['ex']
            or any(not is_canonical_iso_date(d) for d in dates.values())
            or not dates['decision'] < dates['record'] < fact['meeting_date'] < dates['ex'] < dates['end_exclusive']
            or dates['end_exclusive'] > protocol['cutoff']):
        raise ValueError('رخداد، تاریخ یا مبلغ به سند اولیه پرونده پیوند ندارد')
    prices = _load_prices(folder / 'prices.parquet', protocol['cutoff'])
    if (set(protocol['input_sha256']) != {'prices.parquet', 'selection.parquet', 'calendar.json'}
            or any(_sha(folder / name) != digest for name, digest in protocol['input_sha256'].items())):
        raise ValueError('منشأ ورودی با پروتکل برابر نیست')
    calendar = read_private_report(folder / 'calendar.json')['dates']
    if any(not is_canonical_iso_date(d) or d >= protocol['cutoff'] for d in calendar):
        raise ValueError('تقویم پرونده از مرز عبور کرده است')
    selections = _load_prices(folder / 'selection.parquet', protocol['cutoff'])
    if (set(prices.symbol) != {event.symbol} or selections.to_dict('records') !=
            [dict(date=dates['decision'], symbol=event.symbol, rank=0)]
            or not calendar or calendar[0] != dates['decision'] or calendar[-1] >= dates['end_exclusive']):
        raise ValueError('قیمت/انتخاب/تقویم با دامنه صریح پرونده برابر نیست')
    replayed = _compare(prices, calendar, selections, event, PortfolioConfig(**protocol['config']))
    if _summary(replayed) != result['modes']:
        raise ValueError('بازحساب آمار و دفتر حقوق با نتیجه برابر نیست')
    for mode, replay in replayed.items():
        for label in ('equity', 'executions', 'decisions'):
            expected_frame = _load_prices(folder / f'{mode}-{label}.parquet', protocol['cutoff'], allow_empty=True)
            derived = getattr(replay, label)
            # Parquet نوع Index ستون‌های جدول کاملاً بی‌ستون/بی‌ردیف را حفظ نمی‌کند.
            if not (derived.shape == expected_frame.shape == (0, 0)):
                pd.testing.assert_frame_equal(derived, expected_frame)
    if any(_sha(folder / name) != digest for name, digest in manifest['files'].items()):
        raise ValueError('ورودی هنگام بازبینی تغییر کرد')
    return dict(experiment_id=result['experiment_id'], replay_verified=True, source_comparison_verified=True,
                accepted_for_signals=False, profitability_proven=False, modes=result['modes'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run')
    for name in ('comparison', 'decision-date', 'record-date', 'ex-date', 'end-exclusive'):
        run.add_argument('--' + name, required=True)
    run.add_argument('--capital-rial', type=float)
    run.add_argument('--horizon', type=int, default=1)
    review = commands.add_parser('review')
    review.add_argument('--study', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'review':
            result = review_cash_dividend_case(args.study)
        else:
            from database.db import DatabaseManager

            if not settings.DB_PATH.is_file():
                raise ValueError('دیتابیس معتبر موجود لازم است؛ این مسیر دیتابیس خالی نمی‌سازد')
            with DatabaseManager() as db:
                result = run_cash_dividend_case(db, comparison=args.comparison, decision_date=args.decision_date,
                    record_date=args.record_date, ex_date=args.ex_date, end_exclusive=args.end_exclusive,
                    initial_capital_rial=args.capital_rial, horizon=args.horizon)
    except (ValueError, OSError, KeyError, AssertionError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    import sys

    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
