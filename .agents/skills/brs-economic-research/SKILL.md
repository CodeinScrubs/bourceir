---
name: brs-economic-research
description: Design or review one BRS trading experiment using matched capital, costs, execution and uncertainty; use for strategy comparisons and choosing the next profit-relevant experiment, not generic model shopping.
---

# BRS economic research

Read `AGENTS.md`, [readiness gates](../../../docs/research/research-readiness-gates-fa.md) and the relevant registered study. Read [personal strategy protocol](../../../docs/research/personal_strategy_protocol.md) for the owner's objectives instead of inventing new risk budgets.

## Reuse before searching

Locate prior experiments and archived plans. Match the actual universe, features, decision clock, horizon, execution policy, fees, capital, benchmark and data/code generation. A renamed method or another AI suggestion is not a new experiment. Reexecute when reproduction, corrected inputs or a changed scientific question warrants it.

Register one question and its acceptance/rejection or inconclusive criterion before calculating outcomes. Prefer the smallest experiment that can distinguish selection, timing, sizing, execution or exit effects. Broad ML/RL/evolutionary searches require a concrete untested question and suitable data; a new algorithm name is insufficient.

## Economic comparison

- Match initial holdings/cash, investable universe, session calendar, fee regime, capacity and end/exit window across strategy and benchmark. Use the existing shared-capital execution and ledger modules.
- Keep NAV, liquidated cash, receivables and unsold quantities separate. Best future prices are hindsight opportunity labels, not proof of achievable profit.
- Check point-in-time membership and raw-input compatibility. Today's most active stocks cannot silently become an unbiased historical universe.
- Keep unknown outcomes in the declared denominator. Missing data, modeled queue blocks, partial fills and delayed exits have different meanings. Test whether unresolved paths can change the conclusion; if so the comparison remains inconclusive.
- Use paired, time-aligned returns and uncertainty consistent with overlapping positions and repeated trials. Respect the existing purge/embargo and holdout contracts; code repairs do not restore an already viewed holdout.
- Report net excess return, drawdown, turnover, capital usage and execution stress alongside the owner's documented return target. A trade-average percentage is not a portfolio return.

For the pre-fold benchmark, read [its selection/entry contract](../../../docs/research/prefold-top5-fa.md); do not substitute first-day liquidity selection and attribute the difference solely to timing.

## Move toward evidence that can earn money

If the comparison is economically valid and promising, freeze one rule and record future decisions before results. Read [the existing forward protocol](../../../docs/research/aban07-forward-fa.md) only when operating that trial; verify the exact registered version, never backfill missed days or merge versions.

If evidence rejects the method, retain the failure and stop that branch. If inconclusive, name the material missing input and the next bounded check. An unresolved single event does not require resolving every company's history before any other valid experiment can proceed. Do not change the eligible sample after seeing its returns.
