"""Audit missed opportunities in an existing, fully replayed EXPECTANCY archive."""

import argparse
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path

import pandas as pd

from config import settings
from core.timeutils import now_tehran_iso
from ops.report_files import private_report_path, write_private_report
from ops.research_review import _verify_inventory, read_development_parquet, sha256
from research.expectancy_run import PATTERN, review_expectancy
from research.hunter_audit import (
    SCOPE,
    VERSION,
    checked_opportunities,
    describe_checked_opportunities,
    summarize_opportunities,
)
from research.labels import market_calendar
from research.run import research_code_fingerprint


def _json_bytes(path, expected=None):
    contents = path.read_bytes()
    digest = hashlib.sha256(contents).hexdigest()
    if expected is not None and digest != expected:
        raise ValueError('تصویر گزارش یا مدل هنگام ممیزی تغییر کرده است')
    return json.loads(contents), digest


def _rows_target(path, root):
    target = Path(path).resolve()
    if (target.suffix.lower() != '.parquet'
            or not target.is_relative_to((settings.DATA_DIR / 'private').resolve())
            or target.is_relative_to(root.resolve()) or target.exists()):
        raise ValueError('جزئیات باید فایل Parquet جدید در پوشه خصوصی و بیرون آرشیو پژوهش باشد')
    return target


def _write_opportunity_rows(rows, target):
    """Publish a complete retrospective panel without overwriting any existing file."""
    columns = ['fold', 'date', 'symbol', 'status', 'normal_expected_net', 'stress_expected_net',
               'state', 'resolved_at', 'normal_net', 'stress_net', 'corporate_actions', 'selected']
    table = rows.loc[:, columns].copy()
    known = table.state.ne('unknown')
    for scenario in ('normal', 'stress'):
        for label, threshold in (('positive', 0.), ('above_3pct', .03), ('above_10pct', .10)):
            table[f'{scenario}_missed_{label}'] = (
                ~table.selected & table[scenario+'_net'].gt(threshold)).astype('boolean').mask(~known)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    primary_error = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.'+target.name+'.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
        table.to_parquet(temporary, index=False)
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        expected = sha256(temporary)
        # link is atomic and exclusive on the supported local filesystems. A
        # failure leaves the old destination intact; there is no overwrite fallback.
        os.link(temporary, target)
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as error:
                if primary_error is None:
                    raise
                primary_error.add_note(f'Temporary opportunity-table cleanup also failed ({type(error).__name__})')
    return dict(path=str(target), sha256=expected, rows=len(table), columns=list(table),
                retrospective=True, all_checked_opportunities_included=True,
                unknown_outcomes_retained=True, selections_are_fills=False,
                corporate_actions_are_synthetic_label_crossings=True)


def run_hunter_audit(db, *, source_id, root=None, rows_output=None):
    """Read-only sidecar, not a new experiment, threshold search or unsealing.

    Full parent review re-fits and replays the registered experiment. Only its
    primary conditional-2d selection is audited, matching the saved outcomes.
    """
    if not isinstance(source_id, str) or not re.fullmatch(PATTERN, source_id):
        raise ValueError('شناسه صریح آزمایش EXPECTANCY لازم است')
    root = Path(root) if root is not None else settings.DATA_DIR / 'research'
    rows_target = _rows_target(rows_output, root) if rows_output is not None else None
    path, folder = root / (source_id + '.json'), root / source_id
    payload, source_hash = _json_bytes(path)
    code_hash = research_code_fingerprint()
    review = review_expectancy(db, source_id, root=root)
    if sha256(path) != source_hash:
        raise ValueError('گزارش منبع هنگام بازبینی تغییر کرد')
    protocol, artifacts = payload['protocol'], payload['artifacts_sha256']
    cutoff = protocol['holdout_start']
    prices = read_development_parquet(folder / 'prices.parquet', artifacts['prices.parquet'], cutoff)
    calendar = market_calendar(prices)
    panels, folds = [], []
    for index, fold in enumerate(protocol['folds']):
        prefix = f'fold-{index:02d}-'
        tables = {kind: read_development_parquet(folder / (prefix+kind+'.parquet'),
                    artifacts[prefix+kind+'.parquet'], cutoff)
                  for kind in ('forecasts', 'outcomes', 'conditional-2d-picks')}
        model_name = prefix+'model.json'
        model, _ = _json_bytes(folder / model_name, payload['models_sha256'][model_name])
        days = calendar[(calendar >= fold['test_start']) & (calendar < fold['test_end_exclusive'])]
        if not len(days):
            raise ValueError('تقویم برش خالی است')
        # Same structural tail exclusion as _fold_jobs; never result-dependent.
        end = days[max(0, len(days)-3)]
        forecast, outcomes, picks = (tables[k] for k in ('forecasts', 'outcomes', 'conditional-2d-picks'))
        if picks.date.ge(end).any():
            raise ValueError('نامزد در انتهای کنارگذاشته‌شده برش وجود دارد')
        mask = forecast.date.lt(end)
        excluded_tail = int((~mask).sum())
        forecast, outcomes = forecast.loc[mask], outcomes.loc[outcomes.date.lt(end)]
        rows = checked_opportunities(forecast, outcomes, picks, model, holdout_start=cutoff)
        panels.append(rows.assign(fold=index))
        folds.append(dict(fold=index, selection_end_exclusive=str(end), excluded_tail_opportunities=excluded_tail,
                          **describe_checked_opportunities(rows)))
    all_rows = pd.concat(panels, ignore_index=True)
    if all_rows.duplicated(['date', 'symbol']).any():
        raise ValueError('فرصت تکراری بین برش‌ها مجاز نیست')
    _verify_inventory(folder, artifacts, parquet=True)
    for name, digest in payload['models_sha256'].items():
        if sha256(folder / name) != digest:
            raise ValueError('مدل هنگام ممیزی تغییر کرد')
    if sha256(path) != source_hash or research_code_fingerprint() != code_hash:
        raise ValueError('گزارش یا کد هنگام ممیزی تغییر کرد')
    details = _write_opportunity_rows(all_rows, rows_target) if rows_target is not None else None
    return dict(version=VERSION, created_at=now_tehran_iso(), source_id=source_id,
        source_report_sha256=source_hash, source_artifacts_sha256=artifacts,
        source_models_sha256=payload['models_sha256'], audit_code_sha256=code_hash,
        source_review=review, accepted_for_signals=False, holdout_eligible=False,
        candidate='conditional-2d', horizon=2, scope=SCOPE,
        corporate_action_model='source synthetic price-reference label chain; not verified cash/rights entitlements',
        all_market_coverage_verified=False, selections_are_fills=False,
        opportunity_rows=details,
        total=summarize_opportunities(all_rows), folds=folds,
        limitations=['not a new strategy or a calibrated probability',
                     'unknown outcomes retained; bounds are not confidence intervals',
                     'row-weighted coverage is not shared-capital portfolio PnL',
                     'excluded illiquid, nontrading and fold-tail rows are not a full-market recall denominator',
                     'forecasted-but-not-selected combines the fixed positive-expectancy gate and rank cap'])


def main(argv=None):
    from database.db import DatabaseManager

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='existing EXPECTANCY experiment ID')
    parser.add_argument('--output', help='JSON path inside data/private/')
    args = parser.parse_args(argv)
    if not re.fullmatch(PATTERN, args.source):
        raise ValueError('شناسه آزمایش EXPECTANCY نامعتبر است')
    if not settings.DB_PATH.is_file():
        raise ValueError('دیتابیس موجود پژوهش لازم است؛ پایگاه خالی ساخته نمی‌شود')
    root = settings.DATA_DIR / 'research'
    source_folder = root / args.source
    forbidden = [settings.DB_PATH, root / (args.source+'.json'), *source_folder.rglob('*')]
    output = private_report_path(args.output or settings.DATA_DIR / 'private' / 'hunter-audits' /
                                 args.source / ('AUDIT-'+uuid.uuid4().hex) / 'report.json', forbidden_paths=forbidden)
    if output.exists():
        raise ValueError('گزارش ممیزی موجود بازنویسی نمی‌شود؛ مسیر تازه بدهید')
    rows_output = _rows_target(output.with_suffix('.opportunities.parquet'), root)
    with DatabaseManager(read_only=True) as db:
        result = run_hunter_audit(db, source_id=args.source, root=root, rows_output=rows_output)
    write_private_report(output, result, forbidden_paths=forbidden)
    print(output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
