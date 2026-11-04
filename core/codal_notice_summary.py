"""Deterministic, evidence-preserving hints for public CODAL document summaries.

The summary is not a complete disclosure, a financial opinion, or a trading signal.
The source's observation time is retained; historical publication remains unverified.
"""

import re

from core.capital_actions import MAX_TEXT_CHARS, primary_capital_registration
from core.rights_evidence import primary_rights_registration
from core.text import normalize_fa, normalize_symbol, to_en_digits

VERSION = 'codal-notice-summary-v1'
EXCERPT_LIMIT = 6000
ANCHORS = re.compile(
    r'افزایش|سرمایه|حق\s*تقدم|مجمع|سود|زیان|درآمد|فروش|هزینه|تولید|نرخ|'
    r'پذیره|ثبت|انتشار|افشا|توقف|بازگشایی|ناظر|تجدید|تاریخ|اصلاح|نسخه|'
    r'میلیون|میلیارد|هزار|ریال|تومان|درصد|[0-9۰-۹٠-٩]'
)


def _excerpts(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    selected = set()
    for index, line in enumerate(lines):
        if ANCHORS.search(normalize_fa(line)):
            selected.update(range(max(0, index-1), min(len(lines), index+3)))
    pieces = []
    used = 0
    truncated = False
    for index in sorted(selected):
        line = lines[index]
        if used + len(line) + 1 > EXCERPT_LIMIT:
            truncated = True
            break
        pieces.append(line)
        used += len(line)+1
    return pieces, truncated, len(lines)


def summarize_codal_notice(manifest, content, *, notice=None, end_exclusive):
    """Summarize archived bytes without asserting unverified issuer or financial facts."""
    if not isinstance(manifest, dict) or manifest.get('format') not in ('text', 'pdf'):
        raise ValueError('A public document manifest with explicit format is required')
    if not isinstance(content, bytes) or len(content) > 25*1024*1024:
        raise ValueError('Bounded document bytes are required')
    if notice is not None and (not isinstance(notice, dict)
            or notice.get('url') != manifest.get('source_url')):
        raise ValueError('Notice metadata must point to the captured source URL')
    result = dict(version=VERSION, source_url=manifest.get('source_url'),
        source_content_sha256=manifest.get('content_sha256'), document_format=manifest['format'],
        source_available_at=manifest.get('available_at'), observed_at=manifest.get('observed_at'),
        tracing_no=notice.get('tracing_no') if notice else None,
        symbol=normalize_symbol(notice['symbol_fa']) if notice and isinstance(notice.get('symbol_fa'), str) else None,
        company_name=notice.get('company_name') if notice else None,
        title=notice.get('title') if notice else None,
        claimed_published_at=notice.get('published_at') if notice else None,
        metadata_origin='public_notice_index' if notice else 'unavailable',
        publication_time_verified=False, historical_identity_verified=False, numeric_facts_reviewed=False,
        source_text_complete_verified=False, summary_complete=False, source_bytes_recoverable=False,
        superseding_tracing_numbers=[], supersession_absence_proves_finality=False,
        excerpts=[], excerpt_truncated=False, source_line_count=None,
        capital_registration=None, rights_registration=None,
        requires_manual_review=True, feature_eligible=False, accepted_for_signals=False)
    if manifest['format'] == 'pdf':
        result['review_reason'] = 'pdf_content_not_interpreted'
        return result
    try:
        text = content.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('The captured text must be UTF-8') from error
    if len(text) > MAX_TEXT_CHARS:
        result['review_reason'] = 'text_exceeds_structured_parser_limit'
        return result
    normalized = normalize_fa(to_en_digits(text)).replace('\u200c', ' ')
    if notice is None:
        lines = [normalize_fa(line) for line in text.splitlines() if line.strip()]
        headings = [match for line in lines if (match := re.fullmatch(r'شرکت (.+?) - نماد:\s*(.+)', line))]
        if len(headings) == 1 and max(len(headings[0][1]), len(headings[0][2])) <= 200:
            result.update(symbol=normalize_symbol(headings[0][2]), company_name=headings[0][1],
                          metadata_origin='unverified_document_heading')
        titles = [match.group(1) for line in lines if (match := re.fullmatch(r'موضوع:\s*(.+)', line))]
        if len(titles) == 1 and len(titles[0]) <= 200:
            result['title'] = titles[0]
    result['superseding_tracing_numbers'] = [int(value) for value in re.findall(
        r'نسخه\s+جدیدتری\s+به\s+شماره\s*\(\s*([0-9]+)\s*\)', normalized)]
    result['excerpts'], result['excerpt_truncated'], result['source_line_count'] = _excerpts(text)
    if notice:
        result['capital_registration'] = primary_capital_registration(text, notice, end_exclusive=end_exclusive)
        result['rights_registration'] = primary_rights_registration(text, notice, end_exclusive=end_exclusive)
    if result['superseding_tracing_numbers']:
        result['review_reason'] = 'newer_version_referenced'
    elif not notice:
        result['review_reason'] = 'notice_metadata_missing'
    elif result['rights_registration']['status'] == 'supported_primary_fact':
        result['review_reason'] = 'parsed_amounts_still_unverified'
    else:
        result['review_reason'] = 'structured_facts_unavailable_or_unverified'
    return result
