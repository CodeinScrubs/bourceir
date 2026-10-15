"""Manual, offline audit of supplied code. Synthetic probes live only in tests.

python -m tests.audit_attachment_prototype ATTACHMENT OUTPUT_JSON
Only selected, inspected pure functions are evaluated; imports, account seeds,
database writers and network functions from the attachment are never executed.
"""

import ast
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from config import settings
from core.text import normalize_fa, normalize_symbol, to_en_digits


def functions(source, names):
    tree = ast.parse(source)
    selected = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    # No top-level statements, imports or decorators from the supplied text.
    for node in ast.walk(ast.Module(body=selected, type_ignores=[])):
        if isinstance(node, ast.Name) and (node.id.startswith('__') or node.id in
                {'open', 'eval', 'exec', 'compile', 'globals', 'locals', 'getattr', 'setattr', 'requests'}):
            raise ValueError('Non-pure function in audit selection')
        if isinstance(node, ast.Attribute) and node.attr.startswith('__'):
            raise ValueError('Dunder access in audit selection')
    scope = dict(Any=Any, Dict=Dict, List=List, Optional=Optional, pd=pd, np=np,
                 re=re, to_en_digits=to_en_digits, normalize_symbol=normalize_symbol, normalize_fa=normalize_fa)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in
                {'FUND_PREFIXES', 'BOND_PREFIXES'} for target in node.targets):
            for target in node.targets:
                scope[target.id] = ast.literal_eval(node.value)
    exec(compile(ast.Module(body=selected, type_ignores=[]), '<selected-pure-functions>', 'exec'), scope)
    return scope


def audit(path):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != 'c37d112ab7e8fdca97277b5649b0324b43ff381bececb62cdc430c7b7a4a2795':
        raise ValueError('Only the exact inspected attachment is supported; do not execute unreviewed code')
    text = raw.decode('utf-8-sig')
    blocks = re.findall(r'```python\s*\n(.*?)\n```', text, re.S)
    compilation = []
    for index, code in enumerate(blocks):
        try:
            compile(code, f'<attachment-python-{index}>', 'exec')
            compilation.append(dict(block=index, compiled=True))
        except SyntaxError as error:
            compilation.append(dict(block=index, compiled=False, error=type(error).__name__, line=error.lineno))
    ledger = next(code for code in reversed(blocks) if 'def enrich_position_accounting' in code)
    scope = functions(ledger, {'enrich_position_accounting', 'compute_parent_rights_combined_status',
                               '_extract_last_float', 'parse_farabixo_multiline_text'})
    accounting = scope['enrich_position_accounting']
    sample = dict(symbol_fa='آزمایشی', quantity=2, current_value_rial=200, net_pnl_rial=100,
                  net_avg_buy_rial=50, breakeven_rial=51, total_buy_cost_rial=150)
    result = accounting(sample)
    division_error = None
    try:
        accounting({**sample, 'net_pnl_rial': 200})
    except ZeroDivisionError as error:
        division_error = type(error).__name__
    smart = next(code for code in blocks if 'def evaluate_filter_historical_edge' in code)
    # Syntax repair only to probe the separate evaluator; it does not make the original module valid.
    repaired = smart.replace('     g = df.groupby', '    g = df.groupby')
    evaluator = functions(repaired, {'evaluate_filter_historical_edge'})['evaluate_filter_historical_edge']
    frame = pd.DataFrame([dict(symbol='آزمایشی', date=f'2024-01-0{i+1}', locked_up=0, locked_down=0,
                              pit_index=1+i/100, vol_ratio_30d=1, legal_buy_share_pct=10,
                              per_capita_buy_m_toman=4 if i == 0 else 1, per_capita_buy_3d=3,
                              per_capita_buy_5d=2, buyer_power_1d=2, net_real_flow_m_toman=1)
                          for i in range(4)])
    unlocked = evaluator(frame, holding_days=1)
    frame.loc[2, 'locked_down'] = 1
    locked = evaluator(frame, holding_days=1)
    registry = next(code for code in reversed(blocks) if 'def _classify_instrument' in code)
    classify = functions(registry, {'_classify_fund_by_desc', '_classify_instrument'})['_classify_instrument']
    seed = next(n.value for n in ast.parse(ledger).body if isinstance(n, ast.AnnAssign)
                and n.target.id == 'FARABIXO_ALL_47_POSITIONS')
    positions = ast.literal_eval(seed)  # Literal inspection only; no database seeding.
    return dict(attachment_sha256=hashlib.sha256(raw).hexdigest(), lines=len(text.splitlines()),
                python_compilation=compilation,
                explicit_cost_ignored=dict(expected=150, returned=result['invested_cost_rial']),
                zero_denominator_with_positive_explicit_cost=division_error,
                exit_lock_changes_sample_size=dict(unlocked=unlocked['sample_size'], locked=locked['sample_size']),
                scanner_uses_market_comparisons='df["mkt_avg_per_capita_buy"]' in repaired,
                evaluator_uses_market_comparisons='mkt_avg_per_capita' in ast.get_source_segment(
                    repaired, next(n for n in ast.parse(repaired).body
                                   if isinstance(n, ast.FunctionDef) and n.name == 'evaluate_filter_historical_edge')),
                unknown_instrument_declared_tradable=classify('آزمایشی', 'ابزار با هویت تأییدنشده', 'سایر')[2],
                non_gold_commodity_fund_classification=classify('آزمایشی', 'صندوق س. کالای زعفران', 'FUNDS')[0],
                literal_seed_count=len(positions), literal_seed_value_sum=sum(p['current_value_rial'] for p in positions),
                diagnostic_syntax_repair_applied=True, account_seeds_executed=False, network_calls=0)


if __name__ == '__main__':
    destination = Path(sys.argv[2]).resolve()
    if not destination.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('Audit output must stay private')
    result = audit(Path(sys.argv[1]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
