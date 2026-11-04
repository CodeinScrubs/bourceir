"""Byte-bound explicit accounting inputs; evidence labels are not independent verification."""

from dataclasses import fields
from pathlib import Path

from backtest.bonus_shares import BonusCapitalization, BonusFractionSettlement, BonusRelease, _available
from backtest.cash_dividends import CashDividend, DividendPayment, _identifier
from backtest.corporate_actions import BonusDividendBook, DatedCashDividendBook
from config import settings
from core.text import normalize_symbol
from core.timeutils import now_tehran
from ops.report_files import read_private_report
from research.anniversary_run import sha

VERSION = 'corporate-action-inputs-v1'
KINDS = {
    'bonuses': (BonusCapitalization, 'issuer_disclosure'),
    'dividends': (CashDividend, 'issuer_disclosure'),
    'payments': (DividendPayment, 'holder_cash_receipt'),
    'releases': (BonusRelease, 'holder_share_credit'),
    'fraction_settlements': (BonusFractionSettlement, 'holder_cash_receipt'),
}
ROLES = {'issuer_disclosure', 'holder_cash_receipt', 'holder_share_credit', 'coverage_review'}
CASE_KEYS = {'symbol', 'ins_code', 'coverage', 'coverage_note', 'coverage_evidence_ids', *KINDS}


def evidence_path(root, value):
    if (not isinstance(value, str) or not value or '\\' in value
            or Path(value).is_absolute() or '..' in Path(value).parts):
        raise ValueError('Evidence must use a contained relative path')
    target = (root / value).resolve()
    if (not target.is_relative_to(root.resolve()) or target.suffix.lower() not in {'.txt', '.pdf', '.html', '.json'}
            or target == settings.DB_PATH.resolve()
            or (target.exists() and settings.DB_PATH.exists() and target.samefile(settings.DB_PATH))):
        raise ValueError('Evidence path escapes its input folder or is not an allowed document')
    return target


def load_inputs(path, selected):
    """Bind every selected native identity and every referenced document before prices are read."""
    path = Path(path).resolve()
    payload = read_private_report(path)
    if (set(payload) != {'version', 'price_unit', 'cases', 'evidence'}
            or payload['version'] != VERSION or payload['price_unit'] != 'Rial'
            or not isinstance(payload['cases'], list) or not isinstance(payload['evidence'], list)
            or not 0 <= len(payload['evidence']) <= 200):
        raise ValueError('Versioned explicit Rial accounting input required')
    expected = {row['symbol']: str(row['ins_code']) for row in selected}
    if len(expected) != len(selected) or len(payload['cases']) != len(expected):
        raise ValueError('All original selected sleeves must remain in the accounting input')
    documents, total_bytes = {}, 0
    for item in payload['evidence']:
        if not isinstance(item, dict) or set(item) != {'id', 'path', 'sha256', 'available_at', 'role'}:
            raise ValueError('Explicit evidence identity, path, hash, receipt time and role required')
        _identifier(item['id'])
        if item['id'] in documents or item['role'] not in ROLES:
            raise ValueError('Duplicate evidence identity or unknown evidence role')
        stamp = _available(item['available_at'])
        if stamp > now_tehran().isoformat():
            raise ValueError('Evidence receipt cannot be in the future')
        source = evidence_path(path.parent, item['path'])
        size = source.stat().st_size
        total_bytes += size
        if not 0 < size <= 16 * 1024 * 1024 or total_bytes > 64 * 1024 * 1024 or sha(source) != item['sha256']:
            raise ValueError('Evidence bytes, size or hash differ from the input declaration')
        documents[item['id']] = dict(item, source=source)
    seen = set()
    for case in payload['cases']:
        if not isinstance(case, dict) or set(case) != CASE_KEYS:
            raise ValueError('Each sleeve requires explicit coverage and all five action lists')
        symbol = normalize_symbol(case['symbol'])
        if (symbol in seen or symbol not in expected or case['symbol'] != symbol
                or type(case['ins_code']) is not str or case['ins_code'] != expected[symbol]):
            raise ValueError('Accounting input identity differs from the frozen selection')
        seen.add(symbol)
        if (case['coverage'] not in ('partial', 'declared_complete')
                or not isinstance(case['coverage_note'], str) or not 10 <= len(case['coverage_note']) <= 2000
                or not isinstance(case['coverage_evidence_ids'], list)
                or any(key not in documents for key in case['coverage_evidence_ids'])):
            raise ValueError('Explicit event-coverage scope and explanation required')
        if case['coverage'] == 'declared_complete' and not any(
                documents[key]['role'] == 'coverage_review' for key in case['coverage_evidence_ids']):
            raise ValueError('Declared complete coverage requires a separate coverage review document')
        for kind, (cls, role) in KINDS.items():
            if not isinstance(case[kind], list) or len(case[kind]) > 1000:
                raise ValueError('Bounded explicit accounting action lists required')
            allowed = {field.name for field in fields(cls)} - {'evidence_sha256', 'evidence_available_at'}
            allowed.add('evidence_id')
            if kind == 'dividends':
                allowed.add('previous_price_session')
            for event in case[kind]:
                if not isinstance(event, dict) or set(event) != allowed:
                    raise ValueError('Accounting event fields differ from the versioned contract')
                document = documents.get(event['evidence_id'])
                if document is None or document['role'] != role:
                    raise ValueError('Issuer schedules cannot substitute for holder cash/credit receipts')
                if 'symbol' in event and event['symbol'] != symbol:
                    raise ValueError('Event issuer differs from its frozen sleeve')
    return payload, documents


def accounting_book(case, documents):
    """Do not infer a payment, release, entitlement ratio, or rounding convention."""
    built, predecessors = {}, {}
    for kind, (cls, _) in KINDS.items():
        built[kind] = []
        for value in case[kind]:
            value = dict(value)
            document = documents[value.pop('evidence_id')]
            if kind == 'dividends':
                predecessors[value['event_id']] = value.pop('previous_price_session')
            value['evidence_sha256'] = document['sha256']
            if 'evidence_available_at' in {field.name for field in fields(cls)}:
                value['evidence_available_at'] = document['available_at']
            built[kind].append(cls(**value))
    cash = DatedCashDividendBook(built['dividends'], built['payments'], previous_price_sessions=predecessors)
    return BonusDividendBook(built['bonuses'], built['releases'], built['fraction_settlements'], dividends=cash)
