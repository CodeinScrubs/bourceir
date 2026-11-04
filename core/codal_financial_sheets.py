"""Discover explicitly marked sheet options in a Codal HTML response.

This parser performs no I/O and does not establish the identity, publication
time, historical availability, or correctness of a document.  In
particular, an unmarked option list is not evidence that its first option was
the sheet actually served by Codal.
"""

import re
from html.parser import HTMLParser

MAX_HTML_CHARS = 20_000_000
MAX_SHEETS = 256
MAX_TITLE_CHARS = 4096
_ID = re.compile(r"-?(?:0|[1-9][0-9]{0,9})\Z", re.ASCII)


class _SheetParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = False
        self.inside = False
        self.current = None
        self.sheets = []
        self.ids = set()

    def _finish_option(self):
        if self.current is None:
            return
        title = " ".join("".join(self.current.pop("_text")).split())
        if len(title) > MAX_TITLE_CHARS:
            raise ValueError("sheet title exceeds limit")
        self.current["title"] = title or None
        self.sheets.append(self.current)
        self.current = None

    def handle_starttag(self, tag, attrs):
        if self.inside:
            if tag == "select":
                raise ValueError("nested sheet selector")
            if tag == "option":
                self._finish_option()
                if len(self.sheets) >= MAX_SHEETS:
                    raise ValueError("too many sheets")
                values = [value for key, value in attrs if key == "value"]
                selected = [value for key, value in attrs if key == "selected"]
                if len(values) != 1 or len(selected) > 1:
                    raise ValueError("invalid sheet option attributes")
                value = values[0]
                if value is None or not _ID.fullmatch(value) or value == "-0":
                    raise ValueError("invalid sheet id")
                sheet_id = int(value)
                if sheet_id in self.ids:
                    raise ValueError("duplicate sheet id")
                self.ids.add(sheet_id)
                self.current = {"sheet_id": sheet_id, "selected": bool(selected), "_text": []}
            elif self.current is not None:
                raise ValueError("unexpected markup inside sheet option")
            elif tag != "optgroup":
                raise ValueError("unexpected markup inside sheet selector")
            return
        if tag != "select":
            return
        names = [value for key, value in attrs if key == "name"]
        ids = [value for key, value in attrs if key == "id"]
        if "ctl00$ddlTable" not in names and "ddlTable" not in ids:
            return
        if len(names) > 1 or len(ids) > 1:
            raise ValueError("ambiguous selector attributes")
        if names and names[0] != "ctl00$ddlTable":
            raise ValueError("conflicting selector name")
        if ids and ids[0] != "ddlTable":
            raise ValueError("conflicting selector id")
        if self.found:
            raise ValueError("duplicate sheet selector")
        self.found = True
        self.inside = True

    def handle_endtag(self, tag):
        if not self.inside:
            return
        if tag == "option":
            self._finish_option()
        elif tag == "select":
            self._finish_option()
            self.inside = False
        elif tag == "optgroup":
            self._finish_option()

    def handle_data(self, data):
        if self.inside and self.current is not None:
            if sum(map(len, self.current["_text"])) + len(data) > MAX_TITLE_CHARS:
                raise ValueError("sheet title exceeds limit")
            self.current["_text"].append(data)


def discover_financial_sheets(html: str) -> dict:
    """Return observed sheet choices and explicit selection; reject ambiguity.

    Selection is unmarked when no HTML option carries the selected attribute.
    The browser's implicit default is deliberately not substituted.  Sheet
    identifiers and names are observations, not verified financial facts.
    """
    if not isinstance(html, str):
        raise ValueError("html must be a string")
    if len(html) > MAX_HTML_CHARS:
        raise ValueError("html exceeds limit")
    parser = _SheetParser()
    parser.feed(html)
    parser.close()
    if parser.inside:
        raise ValueError("unclosed sheet selector")
    if parser.found and not parser.sheets:
        raise ValueError("empty sheet selector")
    selected = [item["sheet_id"] for item in parser.sheets if item["selected"]]
    if len(selected) > 1:
        raise ValueError("multiple selected sheets")
    return {
        "version": "codal-financial-sheets-v1",
        "status": "observed" if parser.found else "not_present",
        "sheets": parser.sheets,
        "selected_sheet_id": selected[0] if selected else None,
        "selection_status": "selected" if selected else "unmarked",
    }
