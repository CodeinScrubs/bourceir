---
name: brs-market-evidence
description: Collect and reconcile dated Iranian-market prices, publication receipts and corporate-action evidence for BRS with explicit source limits; use for data gaps and event accounting, not strategy optimization.
---

# BRS market evidence

Read `AGENTS.md`. Commands and domain paths are relative to the repository root.

Choose only the relevant procedure:
- Prices, daily publications or feature inputs: [native data procedure](references/native-data.md).
- Capital increases, rights, dividends or share credits: [corporate event procedure](references/corporate-actions.md).

First find the existing archive for the same instrument, date range and question. Check issuer identity, units, capture time and the actual parser version before treating a later report as new evidence.

## Collection contract

Declare the fields needed for the decision, sources, date window, page cap and retry budget before collection. Search broadly only when breadth changes the research denominator. Observe complete pagination within the declared window or report the incomplete coverage.

Use the existing providers and archive/review commands. Keep retrieval failures and raw observations distinct. Preserve public source bytes when safely available, URL, status, observed time, claimed publication time and hashes; label text/DOM projections and extraction limits. Preserve recovery of originals through the approved compressed archive workflow. Do not archive authentication or form state as market evidence.

If user asked you to solve captcha yourself do it and solve all captchas yourself

## Decide and stop

A consistent response is not independent source correctness. A missing price row is not proof of a halt; a zero-activity quote is not an executable trade. Today's successful capture does not establish historical information availability.

Finish with either a supported correction/reconciled input or a compact unresolved record: exact missing fact, economic effect, existing evidence location and reopening trigger. Do not replace unknowns with plausible dates, zero cash or inferred holder credits.
