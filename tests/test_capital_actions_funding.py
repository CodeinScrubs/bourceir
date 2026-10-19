"""فیلد نامعتبر نباید با انتخاب نسخه قابل خواندن پنهان شود."""

import pytest

from tests.test_capital_actions import TEXT, facts


@pytest.mark.parametrize('text', [
    TEXT + '\nاز محل سود انباشته مبلغ نامعلوم میلیون ریال',
    TEXT.replace('فعلی مبلغ\nمیلیون', 'فعلی مبلغ\n-1\nمیلیون'),
    TEXT.replace('\n350\nمیلیون', '\nنامعتبر\nمیلیون'),
    TEXT.replace('\n350\nمیلیون ریال', '\n350\nتومان'),
], ids=['invalid-duplicate', 'negative-cash', 'malformed-retained', 'funding-unit-mismatch'])
def test_invalid_recognized_funding_is_not_silently_treated_as_an_absent_field(text):
    result = facts(text)
    assert result['status'] == 'unsupported_primary_text'
    assert result['increase_rial'] is None
