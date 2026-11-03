"""Bitemporal, descriptive monthly versions; capture and publication are distinct.

Source-claim chronology is a reconstruction diagnostic, never historical feature
eligibility. An unusable latest correction blocks fallback to older numbers.
"""

import hashlib
import json
import re
from urllib.parse import parse_qs, urlsplit

from core.codal_documents import public_document_url
from core.codal_monthly_sales import extract_monthly_sales
from core.codal_revenue import _constant, _unique
from core.codal_search import _native_stamp
from core.opportunity_radar import digest, stamp
from core.text import normalize_symbol, to_en_digits

VERSION = 'codal-monthly-vintage-v2'
FALSE_FLAGS = ('historical_publication_verified', 'historical_issuer_membership_verified',
    'complete_revision_history_verified', 'original_publication_bytes_verified',
    'accepted_for_features', 'accepted_for_signals', 'profitability_proven')


def _normalized(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('explicit issuer or report identity required')
    return normalize_symbol(value)


def _serial(value):
    url = public_document_url(value)
    values = parse_qs(urlsplit(url).query).get('LetterSerial', [])
    if len(values) != 1 or not values[0]:
        raise ValueError('exactly one public report serial required')
    return values[0]


def _visible_identity(text, label):
    lines = [_normalized(line) for line in text.splitlines() if line.strip()]
    indices = [i for i, line in enumerate(lines) if line == label]
    if len(indices) != 1 or indices[0] + 1 >= len(lines):
        raise ValueError('explicit unique visible issuer label required')
    return lines[indices[0] + 1]


def monthly_version(loaded, letter, *, source_bytes, visible_text):
    """Bind preserved cells to one metadata version without backdating receipt."""
    if (not isinstance(source_bytes, bytes) or not 0 < len(source_bytes) <= 20*1024*1024
            or not isinstance(visible_text, str) or not 0 < len(visible_text.encode('utf-8')) <= 20*1024*1024
            or not isinstance(letter.get('native_metadata_sha256'), str)
            or not re.fullmatch('[a-f0-9]{64}', letter['native_metadata_sha256'])):
        raise ValueError('bounded bound monthly source and native metadata digest required')
    manifest, data = loaded['manifest'], loaded['data']
    try:
        decoded = json.loads(source_bytes.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError('bound monthly source JSON required') from error
    if digest(decoded) != digest(data):
        raise ValueError('loaded monthly data differs from the bound source bytes')
    report = data['report']
    if (type(report.get('tracing_no')) is not int or type(letter.get('tracing_no')) is not int
            or report['tracing_no'] != letter['tracing_no']
            or report['tracing_no'] != manifest['expected_tracing_no']):
        raise ValueError('document tracing number differs from native search metadata')
    if hashlib.sha256(source_bytes).hexdigest() != manifest['files_sha256']['tables.json']:
        raise ValueError('monthly cells differ from the bound source bytes')
    if hashlib.sha256(visible_text.encode('utf-8')).hexdigest() != manifest['files_sha256']['report.txt']:
        raise ValueError('visible report differs from the bound source text')
    requested, observed, archived, available = map(stamp, (manifest['requested_at'], manifest['observed_at'],
        manifest['archived_at'], manifest['available_at']))
    if not requested <= observed <= archived <= available:
        raise ValueError('monthly source availability cannot predate receipt or archive')
    source_serial = _serial(letter['url'])
    # Codal's Decision -> MonthlyActivity redirect can spell a '+' in the
    # supplied public serial as OOObOOO. Compare ONLY this observed one-way
    # representation of the expected serial; never normalize arbitrary IDs or
    # infer equivalence from a document's financial values.
    allowed_effective = {source_serial, source_serial.replace('+', 'OOObOOO')}
    if (_serial(manifest['source_url']) != source_serial
            or _serial(manifest['effective_url']) not in allowed_effective):
        raise ValueError('public serial differs across metadata and document')
    symbol, company = _normalized(letter['symbol_fa']), _normalized(letter['company_name'])
    if symbol != _visible_identity(visible_text, 'نماد:') or company != _visible_identity(visible_text, 'شرکت:'):
        raise ValueError('visible issuer differs from native search metadata')
    title = to_en_digits(_normalized(letter['title']))
    periods = re.findall(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', title)
    if (letter.get('letter_code') != 'ن-30' or letter.get('source_estimate') is not False
            or 'گزارش فعالیت ماهانه دوره 1 ماهه منتهی به' not in title
            or periods != [report['period_end_jalali']]):
        raise ValueError('explicit one-month non-estimated metadata period required')
    published = _native_stamp(report.get('claimed_publish_time_jalali'))
    if stamp(published) != stamp(letter['source_published_at']):
        raise ValueError('document publication claim differs from native search metadata')
    if stamp(published) > stamp(manifest['observed_at']):
        raise ValueError('claimed publication follows document receipt')
    published_day = stamp(published).date().isoformat()
    metrics = extract_monthly_sales(source_bytes, expected_sha256=manifest['files_sha256']['tables.json'],
        available_at=manifest['available_at'], observed_at=manifest['observed_at'], archived_at=manifest['archived_at'])
    # Period/time validation must also work when arithmetic is unusable. A bad
    # correction remains a version in the timeline; its predecessor is not used.
    from core.timeutils import jalali_to_iso

    period_iso = jalali_to_iso(report['period_end_jalali'])[:10]
    if published_day < period_iso:
        raise ValueError('claimed publication precedes the report period end')
    text = to_en_digits(normalize_symbol(visible_text))
    previous = re.findall(r'این اطلاعیه اصلاحیه اطلاعیه شماره\s*\(\s*([0-9]+)\s*\)', text)
    following = re.findall(r'این اطلاعیه نسخه جدیدتری به شماره\s*\(\s*([0-9]+)\s*\)', text)
    if (len(previous) > 1 or len(following) > 1 or ('اصلاحیه' in title) != bool(previous)
            or any(int(v) <= 0 or int(v) == report['tracing_no'] for v in previous+following)):
        raise ValueError('ambiguous or missing visible revision identity')
    return dict(version=VERSION, snapshot_id=manifest['snapshot_id'], tracing_no=report['tracing_no'],
        symbol_fa=symbol, company_name=company, period_end_jalali=report['period_end_jalali'],
        period_end_iso=period_iso, source_published_at_claim=published,
        observed_at=manifest['observed_at'], available_at=manifest['available_at'],
        claimed_publication_matches_search_and_document=True, visible_issuer_matches_search=True,
        corrects_tracing_no=int(previous[0]) if previous else None,
        later_revision_banner_tracing_no=int(following[0]) if following else None,
        revision_banners_are_model_inputs=False, metadata_sha256=letter['native_metadata_sha256'],
        table_sha256=manifest['files_sha256']['tables.json'], visible_text_sha256=manifest['files_sha256']['report.txt'],
        effective_serial_representation=('literal' if _serial(manifest['effective_url']) == source_serial
            else 'codal_plus_redirect_spelling'),
        status='observed' if metrics['status'] == 'reconciled' else 'unusable', metrics=metrics,
        **dict.fromkeys(FALSE_FLAGS, False))


def select_monthly_version(versions, *, decision_at, clock='observed'):
    """Latest as known, retaining ambiguity and unusable corrections explicitly.

    observed filters by actual capture availability, then ranks the known report
    versions by their publication claim. Re-capturing an older report later must
    not displace a known correction. source_claim filters by reported publication
    solely to study revision leakage, not for live signals.
    """
    if clock not in ('observed', 'source_claim'):
        raise ValueError('explicit observed or diagnostic source_claim clock required')
    decision = stamp(decision_at)
    if not isinstance(versions, list) or len(versions) > 10000:
        raise ValueError('bounded explicit version list required')
    identities, by_trace = set(), {}
    for version in versions:
        if (version.get('version') != VERSION or any(version.get(k) is not False for k in FALSE_FLAGS)
                or version.get('claimed_publication_matches_search_and_document') is not True
                or version.get('visible_issuer_matches_search') is not True
                or version.get('status') not in ('observed', 'unusable')):
            raise ValueError('bound descriptive monthly version required')
        identities.add((version['symbol_fa'], version['company_name'], version['period_end_iso']))
        trace = version['tracing_no']
        if type(trace) is not int or trace <= 0 or trace in by_trace:
            raise ValueError('explicit unique monthly tracing number required')
        if not stamp(version['source_published_at_claim']) <= stamp(version['observed_at']) <= stamp(version['available_at']):
            raise ValueError('version availability cannot predate publication or receipt')
        if (version['status'] == 'observed') != (version['metrics']['status'] == 'reconciled'):
            raise ValueError('version usability differs from its monthly arithmetic')
        by_trace[trace] = version
    if len(identities) > 1:
        raise ValueError('one issuer and period per revision selection required')
    key = 'available_at' if clock == 'observed' else 'source_published_at_claim'
    candidates = [v for v in versions if stamp(v[key]) <= decision]
    output = dict(version=VERSION, clock=clock, decision_at=decision_at,
        status='not_available', selected_tracing_no=None, selected_snapshot_id=None,
        selected_at=None, total_revenue_rial=None, versions_considered=len(candidates),
        future_versions_excluded=len(versions)-len(candidates), **dict.fromkeys(FALSE_FLAGS, False))
    if not candidates:
        return output
    latest = max(stamp(v['source_published_at_claim']) for v in candidates)
    selected = [v for v in candidates if stamp(v['source_published_at_claim']) == latest]
    if len(selected) != 1:
        return dict(output, status='ambiguous_latest', selected_published_at_claim=latest.isoformat())
    selected = selected[0]
    result = dict(output, status=selected['status'], selected_at=selected[key],
        selected_published_at_claim=selected['source_published_at_claim'],
        selected_tracing_no=selected['tracing_no'], selected_snapshot_id=selected['snapshot_id'])
    if selected['status'] == 'observed':
        result['total_revenue_rial'] = selected['metrics']['total']['amount_rial']
    return result
