"""Offline tests for read-only discovery, not financial interpretation."""

import pytest

from core.codal_financial_sheets import (
    MAX_HTML_CHARS,
    MAX_SHEETS,
    MAX_TITLE_CHARS,
    discover_financial_sheets,
)


def wrap(options):
    return '<select name="ctl00$ddlTable" id="ddlTable">' + options + "</select>"


def test_explicit_selection_and_omitted_option_closing_tags():
    result = discover_financial_sheets(wrap('<option selected="selected" value="1">سود و زیان\n'
                                             '<option value="0">وضعیت مالی'))
    assert result["status"] == "observed"
    assert result["selected_sheet_id"] == 1
    assert result["selection_status"] == "selected"
    assert result["sheets"] == [
        {"sheet_id": 1, "title": "سود و زیان", "selected": True},
        {"sheet_id": 0, "title": "وضعیت مالی", "selected": False},
    ]


def test_unmarked_is_not_the_first_sheet():
    result = discover_financial_sheets(wrap('<option value="0">first</option>'))
    assert result["selected_sheet_id"] is None
    assert result["selection_status"] == "unmarked"


def test_absent_selector_and_unrelated_selector():
    result = discover_financial_sheets('<select id="other"><option value="1">x</option></select>')
    assert result["status"] == "not_present"
    assert result["selection_status"] == "unmarked"
    assert result["sheets"] == []


@pytest.mark.parametrize("body", [
    '<option value="1">a<option value="1">b',
    '<option selected value="1">a<option selected value="2">b',
    '<option value="1" value="2">a',
    '<option selected selected value="1">a',
    '<option value="01">a',
    '<option value="-0">a',
    '<option value="">a',
    '<option>a',
    '<option value="1"><script>bad</script>',
    '',
])
def test_ambiguous_and_invalid_options_rejected(body):
    with pytest.raises(ValueError):
        discover_financial_sheets(wrap(body))


def test_duplicate_or_unclosed_selector_rejected():
    valid = wrap('<option value="1">x')
    with pytest.raises(ValueError):
        discover_financial_sheets(valid + valid)
    with pytest.raises(ValueError):
        discover_financial_sheets('<select id="ddlTable"><option value="1">x')


def test_selector_conflicting_identity_is_not_accepted():
    with pytest.raises(ValueError):
        discover_financial_sheets('<select id="ddlTable" name="other"><option value="1">a</select>')


def test_option_count_is_bounded():
    options = ''.join(f'<option value="{i}">x' for i in range(MAX_SHEETS + 1))
    with pytest.raises(ValueError):
        discover_financial_sheets(wrap(options))


def test_input_and_title_are_bounded():
    with pytest.raises(ValueError):
        discover_financial_sheets("x" * (MAX_HTML_CHARS + 1))
    with pytest.raises(ValueError):
        discover_financial_sheets(wrap('<option value="1">' + "x" * (MAX_TITLE_CHARS + 1)))


def test_non_string_input_is_rejected():
    with pytest.raises(ValueError):
        discover_financial_sheets(None)


def test_entities_empty_title_and_explicit_boolean_attribute():
    result = discover_financial_sheets(wrap('<option value="-1" selected="false">  '
                                            '<option value="1">A &amp; B'))
    assert result["selected_sheet_id"] == -1  # HTML boolean attribute is present
    assert result["sheets"][0]["title"] is None
    assert result["sheets"][1]["title"] == "A & B"
