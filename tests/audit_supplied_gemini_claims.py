"""Audit-only reproductions for the supplied Gemini conversation.

No top-level proposal code, personal defaults, network calls or alert code runs.
Only explicitly selected AST definitions are compiled into isolated namespaces.
Set BRS_GEMINI_ATTACHMENT to the conversation text to repeat these probes.
Assertions document defects, rather than expected product acceptance behavior.
"""

import ast
import hashlib
import json
import math
import os
import re
import sys
import types
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from core import market_rules
from core.text import normalize_symbol, to_en_digits

SOURCE = Path(os.environ["BRS_GEMINI_ATTACHMENT"]) if os.environ.get("BRS_GEMINI_ATTACHMENT") else None
pytestmark = pytest.mark.skipif(SOURCE is None, reason="Private conversation is optional and never committed")
EXPECTED_SHA = "b3fd8488c90c448d629acad4cc51a7412a610e67c7446134af6c2bc25cce3a79"


def evidence(case, **details):
    print("GEMINI_EVIDENCE " + json.dumps(dict(case=case, **details), ensure_ascii=False))


def selected_definitions(block_line, names, **extra):
    raw = SOURCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA
    source = raw.decode()
    for match in re.finditer(r"^```python\s*\n(.*?)^```", source, re.M | re.S):
        line = source[:match.start()].count("\n") + 2
        if line == block_line:
            tree = ast.parse(match.group(1))
            selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                        and node.name in names]
            assert len(selected) == len(names)
            for node in selected:
                if isinstance(node, ast.FunctionDef):
                    node.decorator_list = []
                    # Override all defaults, including personal account amounts.
                    node.args.defaults = [ast.Constant(None) for _ in node.args.defaults]
            module = types.ModuleType(f"audit_proposal_{block_line}_{len(sys.modules)}")
            sys.modules[module.__name__] = module
            module.__dict__.update(dict(np=np, pd=pd, math=math, re=re, json=json,
                dataclass=dataclass, field=field, HTMLParser=HTMLParser, Any=Any, Dict=Dict,
                List=List, Optional=Optional, market_rules=market_rules,
                normalize_symbol=normalize_symbol, to_en_digits=to_en_digits,
                BANK_ANNUAL_RATE_PCT=20., FIXED_INCOME_ANNUAL_PCT=26.5, **extra))
            exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])),
                         f"gemini-poul.txt:block-{block_line}", "exec"), module.__dict__)
            return module
    raise AssertionError(f"Missing code block at line {block_line}")


def surgeon():
    return selected_definitions(2181, {"SurgeonDecision", "evaluate_portfolio_surgeon"})


def benchmark():
    return selected_definitions(4533, {"evaluate_portfolio_vs_benchmarks", "_pit_cum_return_pct"})


def panel(symbols, returns):
    return pd.DataFrame(dict(symbol=symbols, ret_20d=returns, value=[1e6] * len(symbols),
                            buyer_power=[1.] * len(symbols), locked_up=[0] * len(symbols)))


def test_parser_discards_real_symbol_namad():
    mod = selected_definitions(2181, {"_TableRowExtractor", "_clean_number", "parse_farabixo_html"})
    row = "<tr>" + "".join(f"<td>{v}</td>" for v in ["نماد", "100", "1000", "0", "1000", "1100", "110000", "10000"]) + "</tr>"
    result = mod.parse_farabixo_html("<table>" + row + "</table>")
    assert result["positions"] == []
    evidence("parser_discards_namad", supplied_valid_position=1, parsed_positions=0)


def test_missing_market_becomes_hold_strong():
    result = surgeon().evaluate_portfolio_surgeon(
        [dict(symbol="فولاد", quantity=100_000, avg_buy_price_rial=1000)], pd.DataFrame(),
        total_portfolio_rial=None, top_replacement_symbol="آزمایشی")
    assert result[0].verdict_code == "HOLD_STRONG"
    evidence("missing_market_hold_strong", market_rows=0, verdict=result[0].verdict_code)


def test_old_broker_value_with_new_price():
    result = surgeon().evaluate_portfolio_surgeon(
        [dict(symbol="فولاد", quantity=100_000, avg_buy_price_rial=1000,
              current_price_rial=1000, current_value_rial=100_000_000)],
        pd.DataFrame([dict(symbol_fa="فولاد", last_price=2000)]),
        total_portfolio_rial=None, top_replacement_symbol="آزمایشی")
    decision = result[0]
    assert decision.current_price_rial == 2000
    assert decision.current_value_toman == 10_000_000
    assert decision.weight_in_portfolio_pct == 50.
    evidence("stale_broker_value", current_price=decision.current_price_rial,
             reported_weight_pct=decision.weight_in_portfolio_pct,
             correct_single_holding_weight_pct=100.)


def test_negative_basis_declares_zero_risk():
    result = surgeon().evaluate_portfolio_surgeon(
        [dict(symbol="فولاد", quantity=100_000, avg_buy_price_rial=1000, raw_breakeven_rial=-100)],
        pd.DataFrame(), total_portfolio_rial=None, top_replacement_symbol="آزمایشی")
    assert result[0].pnl_pct == 100.
    assert any("ریسک صفر" in note for note in result[0].educational_notes)
    evidence("negative_basis_zero_risk_claim", fixed_pnl_pct=result[0].pnl_pct,
             current_market_value_rial=100_000_000)


def test_six_rows_accepted_as_240_session_return():
    frame = pd.DataFrame(dict(close_raw=[101.] * 6, close_final_raw=[101.] * 6,
                              ref_price_raw=[100.] * 6))
    result = benchmark()._pit_cum_return_pct(frame, 240)
    assert result is not None and result == round((1.01 ** 6 - 1) * 100, 2)
    evidence("short_history_one_year", available_sessions=6, requested_sessions=240,
             reported_pct=result)


def test_current_weights_distort_historical_portfolio_return():
    positions = [dict(symbol="الف", current_value_rial=200), dict(symbol="ب", current_value_rial=100)]
    result = benchmark().evaluate_portfolio_vs_benchmarks(positions,
        panel(["الف", "ب"], [1., 0.]), dict(bank_20d_pct=1., gold_20d_pct=2., hurdle_20d_pct=2.),
        net_worth_rial=300.)
    assert result["portfolio_20d_pct"] == 66.67
    evidence("end_weights_bias", true_initial_equal_weight_return_pct=50.,
             reported_portfolio_return_pct=result["portfolio_20d_pct"])


def test_beating_all_ignores_dollar():
    result = benchmark().evaluate_portfolio_vs_benchmarks(
        [dict(symbol="الف", current_value_rial=100)], panel(["الف"], [.06]),
        dict(bank_20d_pct=1., gold_20d_pct=2., usd_1m_pct=10., hurdle_20d_pct=10.),
        net_worth_rial=100.)
    assert result["status"] == "BEATING_ALL" and len(result["lagging_positions"]) == 1
    evidence("beating_all_without_beating_dollar", portfolio_return_pct=6.,
             dollar_return_pct=10., status=result["status"], lagging_positions=1)


def test_new_dashboard_requires_missing_numpy_import():
    source = SOURCE.read_bytes().decode('utf-8')
    match = next(m for m in re.finditer(r"^```python\s*\n(.*?)^```", source, re.M | re.S)
                 if source[:m.start()].count("\n") + 2 == 3616)
    tree = ast.parse(match.group(1))
    imported = {a.asname or a.name.split(".")[0] for n in ast.walk(tree)
                if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
    referenced = sum(isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id == "np"
                     for n in ast.walk(tree))
    assert "np" not in imported and referenced >= 2
    evidence("dashboard_missing_numpy", numpy_alias_imported=False, alias_references=referenced)


def test_duplicate_migration_number_rejected(tmp_path, monkeypatch):
    from database import migrator

    (tmp_path / "008_personal_workbench.sql").write_text("SELECT 1;")
    (tmp_path / "008_command_center.sql").write_text("SELECT 1;")
    monkeypatch.setattr(migrator, "MIGRATIONS_DIR", tmp_path)
    with pytest.raises(RuntimeError, match="تکراری"):
        migrator.discover_migrations()
    evidence("migration_008_collision", duplicate_versions=[8, 8], migration_rejected=True)


def test_proposed_fallback_fabricates_locked_day():
    prices = pd.DataFrame(dict(last_price=[103.], close_final_raw=[101.], ref_price_raw=[100.], volume=[1000]))
    invented_range = prices.rename(columns={"last_price": "high_raw"}).assign(low_raw=lambda x: x["high_raw"])
    invented = market_rules.lock_flags(invented_range)
    observed_range = pd.DataFrame(dict(high_raw=[103.], low_raw=[99.], ref_price_raw=[100.], volume=[1000]))
    observed = market_rules.lock_flags(observed_range)
    assert bool(invented.iloc[0]["locked_up"]) and not bool(observed.iloc[0]["locked_up"])
    evidence("fallback_fabricated_lock", actual_low=99., actual_high=103.,
             fabricated_low=103., fabricated_high=103., incorrectly_marked_locked=True)
