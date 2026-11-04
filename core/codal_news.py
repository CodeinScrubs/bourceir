"""Pure, bounded Codal news decoding and replay of approved public JSON bytes."""

import base64
import binascii
import hashlib
import json

from core.codal_documents import public_document_url
from core.codal_search import _native_stamp
from core.opportunity_radar import stamp
from core.text import normalize_fa, normalize_symbol, to_en_digits
from core.timeutils import jalali_to_iso

VERSION = 'codal-news-native-v1'
MAX_BYTES = 1024 * 1024
MAX_ROWS = 100
PUBLIC_FIELDS = frozenset(('AttachmentUrl', 'CompanyName', 'ExcelUrl', 'HasAttachment', 'HasExcel',
    'HasHtml', 'HasPdf', 'HasXbrl', 'IsEstimate', 'LetterCode', 'PdfUrl', 'PublishDateTime',
    'SentDateTime', 'SuperVision', 'Symbol', 'TedanUrl', 'Title', 'TracingNo', 'UnderSupervision',
    'Url', 'XbrlUrl'))


class NewsDecodeError(ValueError):
    def __init__(self, stage, code):
        self.stage, self.code = stage, code
        super().__init__(code)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate news JSON key')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('Nonfinite news JSON number')


def _link(value):
    if not value:
        return None
    try:
        return public_document_url(value)
    except (ValueError, TypeError):
        return None


def normalize_letter(letter):
    """Preserve existing normalization; unparseable native values remain unknown."""
    try:
        return dict(tracing_no=int(letter['TracingNo']), symbol_fa=normalize_symbol(letter.get('Symbol') or ''),
            company_name=normalize_fa(letter.get('CompanyName') or ''), title=normalize_fa(letter.get('Title') or ''),
            letter_code=to_en_digits(letter.get('LetterCode') or '').strip() or None,
            sent_at=jalali_to_iso(letter['SentDateTime']) if letter.get('SentDateTime') else None,
            published_at=jalali_to_iso(letter['PublishDateTime']) if letter.get('PublishDateTime') else None,
            url=_link(letter.get('Url')), attachment_url=_link(letter.get('AttachmentUrl')),
            pdf_url=_link(letter.get('PdfUrl')))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return None


def _public_fields(payload):
    """Unknown fields are not archived; cookies, headers and challenge HTML never enter this receipt."""
    if set(payload) != {'IsAttacker', 'Total', 'Page', 'Letters'}:
        return False
    for row in payload['Letters']:
        if not isinstance(row, dict) or not set(row) <= PUBLIC_FIELDS:
            return False
        for key, value in row.items():
            if key != 'SuperVision':
                if value is not None and type(value) not in (str, int, bool):
                    return False
            elif value is not None:
                if not isinstance(value, dict) or not set(value) <= {'UnderSupervision', 'AdditionalInfo', 'Reasons'}:
                    return False
                if any(type(v) not in (str, int, bool, type(None)) for k, v in value.items() if k != 'Reasons'):
                    return False
                if 'Reasons' in value and (not isinstance(value['Reasons'], list)
                        or any(type(v) not in (str, int, bool, type(None)) for v in value['Reasons'])):
                    return False
    return True


def parse_news_body(body, *, page):
    """Never sort source rows, substitute sent time, or claim complete publication coverage."""
    if type(page) is not int or not 1 <= page <= 100 or not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise ValueError('Bounded native page identity and bytes are required')
    try:
        text = body.decode('utf-8-sig')
    except UnicodeError as error:
        raise NewsDecodeError('decode', 'body_encoding_invalid') from error
    try:
        payload = json.loads(text, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise NewsDecodeError('json', 'json_invalid') from error
    if isinstance(payload, dict) and payload.get('IsAttacker') is True:
        raise NewsDecodeError('schema', 'native_verification_response')
    if (not isinstance(payload, dict) or payload.get('IsAttacker') is not False
            or not isinstance(payload.get('Letters'), list) or len(payload['Letters']) > MAX_ROWS):
        raise NewsDecodeError('schema', 'native_schema_invalid')
    total, pages = payload.get('Total'), payload.get('Page')
    if (type(total) is not int or total < 0 or type(pages) is not int or pages < 0
            or len(payload['Letters']) > total
            or not payload['Letters'] and total > 0 and page <= pages):
        raise NewsDecodeError('pagination', 'pagination_claims_invalid')
    letters, invalid = [], 0
    for item in payload['Letters']:
        normalized = None
        if (isinstance(item, dict) and type(item.get('TracingNo')) is int and item['TracingNo'] > 0
                and isinstance(item.get('Title'), str) and item['Title'].strip()
                and isinstance(item.get('Symbol'), str)):
            normalized = normalize_letter(item)
            if normalized is not None:
                try:
                    # Reuse the historical decoder's full-second/DST checks; a
                    # date-only field must not silently become midnight.
                    normalized['published_at'] = _native_stamp(item.get('PublishDateTime'))
                    sent = item.get('SentDateTime')
                    normalized['sent_at'] = None if sent in (None, '') else _native_stamp(sent)
                    if normalized['sent_at'] is not None and stamp(normalized['sent_at']) > stamp(normalized['published_at']):
                        raise ValueError('Send time follows publication')
                except ValueError:
                    normalized = None
        if normalized is None:
            invalid += 1
        else:
            letters.append(normalized)
    return dict(status='partial' if invalid else 'observed', letters=letters, invalid_rows=invalid,
        raw_count=len(payload['Letters']), total_claim=total, page_count_claim=pages), _public_fields(payload)


def native_evidence(body=None, *, reason='response_unavailable'):
    return dict(version=VERSION, status='captured' if body is not None else 'not_captured',
        reason=None if body is not None else reason,
        body_base64=base64.b64encode(body).decode('ascii') if body is not None else None)


def verify_page_evidence(page):
    """Absence means legacy evidence; present evidence must reproduce every decoded row exactly."""
    if 'native_evidence' not in page:
        return False
    evidence = page['native_evidence']
    if (not isinstance(evidence, dict) or set(evidence) != {'version', 'status', 'reason', 'body_base64'}
            or evidence['version'] != VERSION):
        raise ValueError('Unsupported native news evidence')
    if evidence['status'] == 'not_captured':
        if evidence['body_base64'] is not None or evidence['reason'] not in ('response_unavailable', 'public_schema_unapproved'):
            raise ValueError('Invalid unavailable native news evidence')
        if evidence['reason'] == 'response_unavailable' and page.get('status') in ('observed', 'partial'):
            raise ValueError('Observed native news cannot claim an unavailable response')
        return False
    if (evidence['status'] != 'captured' or evidence['reason'] is not None
            or not isinstance(evidence['body_base64'], str) or len(evidence['body_base64']) > 4*((MAX_BYTES+2)//3)):
        raise ValueError('Invalid bounded native news body')
    try:
        body = base64.b64decode(evidence['body_base64'], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError('Invalid native news base64') from error
    number = page.get('requested_page')
    if (page.get('url') != f'https://search.codal.ir/api/search/v2/q?&PageNumber={number}'
            or type(page.get('http_status')) is not int or page['http_status'] != 200
            or page.get('response_complete') is not True or type(page.get('response_bytes')) is not int
            or page['response_bytes'] != len(body) or page.get('response_sha256') != hashlib.sha256(body).hexdigest()
            or page.get('failure_stage') is not None or page.get('failure_code') is not None
            or stamp(page.get('received_at')) < stamp(page.get('requested_at'))):
        raise ValueError('Native news transport or fingerprint differs')
    parsed, approved = parse_news_body(body, page=number)
    if not approved or json.dumps(parsed, sort_keys=True, ensure_ascii=False) != json.dumps(
            {key: page.get(key) for key in parsed}, sort_keys=True, ensure_ascii=False):
        raise ValueError('Native news normalization differs from retained public bytes')
    return True
