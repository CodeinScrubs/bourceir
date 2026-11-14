"""Resolve explicit Codal revision edges without selecting a convenient amount."""

import hashlib
import re
from datetime import datetime
from decimal import Decimal

from core.assembly_dividend import FAMILY, primary_assembly_dividend
from core.text import normalize_fa, normalize_symbol, to_en_digits
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'assembly-correction-chains-v1'
MAX_NOTICES = 256


def resolve_assembly_corrections(records, *, expected_tracing_numbers, end_exclusive):
    """Retain every version and require every observed leaf to have a known DPS.

    The expected inventory comes from discovery, including unfetched notices.
    A budget limit, unsupported latest document, fork, or missing parent blocks
    resolution. Independent summary/decision roots must agree within a meeting.
    This verifies the supplied graph, not the completeness of Codal's history.
    """
    if (not isinstance(records, list) or len(records) > MAX_NOTICES
            or not isinstance(expected_tracing_numbers, list) or len(expected_tracing_numbers) > MAX_NOTICES
            or any(type(n) is not int or n <= 0 for n in expected_tracing_numbers)
            or len(set(expected_tracing_numbers)) != len(expected_tracing_numbers)
            or not is_canonical_iso_date(end_exclusive)):
        raise ValueError('A bounded unique notice inventory and canonical cutoff are required')
    result = dict(version=VERSION, status='unresolved', groups=[], notices=[], reasons=[],
        duplicate_identical_records=0, expected_notices=len(expected_tracing_numbers), missing_tracing_numbers=[],
        original_publication_time_verified=False, discovery_completeness_verified=False,
        entitlement_verified=False, settlement_verified=False, accepted_for_signals=False)
    nodes, problems = {}, []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get('notice'), dict):
            raise ValueError('Every record needs its native notice metadata')
        notice, text = record['notice'], record.get('text')
        tracing = notice.get('tracing_no')
        if type(tracing) is not int or tracing not in expected_tracing_numbers:
            raise ValueError('Record is outside the declared inventory')
        node = dict(tracing_no=tracing, prior_tracing_no=None, fact=None, status='unavailable',
            text_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest() if isinstance(text, str) else None,
            notice=notice)
        # A failed capture or truncated text cannot leave an older fact authoritative.
        if record.get('capture_status') == 'captured' and isinstance(text, str):
            fact = primary_assembly_dividend(text, notice, end_exclusive=end_exclusive)
            node.update(fact=fact, status=fact['status'])
            joined = normalize_fa(to_en_digits(text))
            parents = re.findall(r'این اطلاعیه اصلاحیه اطلاعیه شماره\s*\(\s*([0-9]+)\s*\)', joined)
            corrected = 'اصلاحیه' in notice.get('title', '')
            parent = int(parents[0]) if len(parents) == 1 else None
            if corrected and (parent is None or not 0 < parent < tracing):
                problems.append(dict(tracing_no=tracing, reason='invalid_revision_reference'))
            elif not corrected and parents:
                problems.append(dict(tracing_no=tracing, reason='unmarked_revision_reference'))
            else:
                node['prior_tracing_no'] = parent
        if tracing in nodes:
            if nodes[tracing] != node:
                problems.append(dict(tracing_no=tracing, reason='conflicting_duplicate_notice'))
            else:
                result['duplicate_identical_records'] += 1
        else:
            nodes[tracing] = node
    missing = sorted(set(expected_tracing_numbers) - set(nodes))
    result['missing_tracing_numbers'] = missing
    if missing:
        problems.append(dict(reason='missing_discovered_notices', tracing_numbers=missing))
    children = {n: [] for n in nodes}
    for tracing, node in nodes.items():
        parent = node['prior_tracing_no']
        fact = node['fact']
        if parent is None:
            continue
        if parent not in nodes:
            problems.append(dict(tracing_no=tracing, reason='missing_revision_parent', parent=parent))
            continue
        children[parent].append(tracing)
        older = nodes[parent]['fact']
        if not fact or not older:
            continue
        identity = ('symbol', 'meeting_date', 'fiscal_year_end', 'statement_form')
        if (any(fact[k] != older[k] for k in identity)
                or normalize_symbol(node['notice'].get('company_name')) != normalize_symbol(
                    nodes[parent]['notice'].get('company_name'))):
            problems.append(dict(tracing_no=tracing, reason='revision_identity_conflict', parent=parent))
        try:
            older_time = datetime.fromisoformat(nodes[parent]['notice']['published_at'])
            newer_time = datetime.fromisoformat(node['notice']['published_at'])
            if (older_time.utcoffset() is None or newer_time.utcoffset() is None
                    or newer_time <= older_time):
                raise ValueError('Revision must be published strictly after its parent')
        except (ValueError, TypeError, KeyError):
            problems.append(dict(tracing_no=tracing, reason='revision_time_conflict', parent=parent))
    for tracing, descendants in children.items():
        fact = nodes[tracing]['fact']
        identity_fields = ('symbol', 'meeting_date', 'fiscal_year_end', 'statement_form', 'stated_published_at')
        if (not fact or any(fact[k] is None for k in identity_fields)
                or (not descendants and fact['status'] != 'supported_primary_fact')):
            problems.append(dict(tracing_no=tracing, reason='unsupported_or_unavailable_document'))
        if len(descendants) > 1:
            problems.append(dict(tracing_no=tracing, reason='ambiguous_revision_fork', children=descendants))
    groups = {}
    for tracing, node in sorted(nodes.items()):
        node['superseded_by'] = sorted(children[tracing])
        node['is_leaf'] = not children[tracing]
        result['notices'].append(node)
        fact = node['fact']
        if not fact or fact['status'] != 'supported_primary_fact':
            continue
        native = node['notice']
        published = datetime.fromisoformat(native['published_at']).astimezone(TEHRAN_TZ)
        if (normalize_symbol(native['symbol_fa']) != fact['symbol'] or not native['title'].lstrip().startswith(
                (FAMILY, 'خلاصه ' + FAMILY)) or published.date().isoformat() >= end_exclusive):
            problems.append(dict(tracing_no=tracing, reason='notice_fact_identity_or_cutoff_conflict'))
        group = groups.setdefault((fact['symbol'], fact['meeting_date'], fact['fiscal_year_end']), [])
        if node['is_leaf']:
            group.append(node)
    for (symbol, meeting, fiscal), leaves in groups.items():
        amounts = {Decimal(node['fact']['dps_rial']) for node in leaves}
        issuers = {normalize_symbol(node['notice']['company_name']) for node in leaves}
        if len(issuers) > 1:
            problems.append(dict(symbol=symbol, meeting_date=meeting, reason='independent_leaf_issuer_conflict'))
        if len(amounts) != 1:
            problems.append(dict(symbol=symbol, meeting_date=meeting, reason='independent_leaf_value_conflict'))
        result['groups'].append(dict(symbol=symbol, meeting_date=meeting, fiscal_year_end=fiscal,
            effective_tracing_numbers=[node['tracing_no'] for node in leaves],
            dps_rial=str(next(iter(amounts))) if len(amounts) == 1 else None,
            observed_chain_resolved=False))
    result['reasons'] = problems
    if nodes and not problems:
        result['status'] = 'resolved_observed_chains'
        for group in result['groups']:
            group['observed_chain_resolved'] = True
    else:
        # A missing later version can invalidate every apparent final amount.
        for group in result['groups']:
            group['dps_rial'] = None
    return result
