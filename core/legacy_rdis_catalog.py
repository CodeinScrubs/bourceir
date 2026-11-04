"""Parse historical public archive form options without inferring market membership."""

import re
from html.parser import HTMLParser

from core.text import normalize_fa

VERSION = 'legacy-rdis-form-catalog-v1'
MAX_BYTES = 2 * 1024 * 1024
_TARGETS = {'companyid', 'announcementtypeid', 'anyear'}


class _Options(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = {}
        self.section = None
        self.option = None

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if tag in ('select', 'option') and len(attrs) != len(attributes):
            raise ValueError('Duplicate source attributes are ambiguous')
        if tag == 'select':
            name = (attrs.get('name') or attrs.get('id') or '').lower()
            if self.section is not None:
                raise ValueError('Nested select is unsupported')
            if name in _TARGETS:
                if name in self.sections:
                    raise ValueError('Duplicate source select')
                self.sections[name] = []
                self.section = name
        elif tag == 'option' and self.section is not None:
            if self.option is not None:
                raise ValueError('Nested option is unsupported')
            self.option = {'value': attrs.get('value'), 'label_parts': []}

    def handle_data(self, data):
        if self.option is not None:
            self.option['label_parts'].append(data)

    def handle_endtag(self, tag):
        if tag == 'option' and self.option is not None:
            option = self.option
            self.sections[self.section].append({'value': option['value'],
                                                'label': normalize_fa(''.join(option['label_parts']))})
            self.option = None
        elif tag == 'select' and self.section is not None:
            if self.option is not None:
                raise ValueError('Unclosed source option')
            self.section = None


def parse_legacy_catalog(content):
    """Extract form choices only; they are not verified securities or events."""
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_BYTES:
        raise ValueError('A bounded archived HTML response is required')
    try:
        html = content.decode('cp1256')
    except UnicodeError as error:
        raise ValueError('Unsupported source encoding') from error
    parser = _Options()
    parser.feed(html)
    parser.close()
    if parser.section is not None or set(parser.sections) != _TARGETS:
        raise ValueError('Incomplete historical form')
    sections = {}
    for key, options in parser.sections.items():
        values = set()
        accepted = []
        for option in options:
            value, label = option['value'], option['label']
            if not isinstance(value, str) or not re.fullmatch(r'0|[1-9][0-9]{0,8}', value):
                raise ValueError('Source option ID must be an explicit integer')
            if value == '0' and not label:
                continue
            if value == '0' or value in values or not label or len(label) > 512:
                raise ValueError('Ambiguous source form option')
            values.add(value)
            accepted.append({'source_id': value, 'label': label})
        if not accepted:
            raise ValueError('A nonempty source option set is required')
        sections[key] = accepted
    years = sections['anyear']
    if any(not re.fullmatch(r'1[34][0-9]{2}', row['source_id'])
           or row['source_id'] != row['label'] for row in years):
        raise ValueError('Source year values are ambiguous')
    return {'version': VERSION, 'companies': sections['companyid'],
            'report_types': sections['announcementtypeid'], 'years': years,
            'company_count': len(sections['companyid']),
            'historical_exchange_membership_verified': False,
            'symbol_or_instrument_identity_verified': False,
            'report_availability_or_completeness_verified': False,
            'capital_increase_events_count': None, 'accepted_for_signals': False}
