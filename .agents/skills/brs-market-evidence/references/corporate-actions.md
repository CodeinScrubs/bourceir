# Corporate event evidence

Use the existing registration tools in `core/capital_actions.py`,
`ops/capital_actions.py` and the relevant ledgers:
`backtest/bonus_shares.py`, `backtest/cash_dividends.py`,
`backtest/subscription_rights.py`. Read
[capital evidence](../../../../docs/research/capital-actions-evidence-fa.md)
before changing parser/accounting assumptions. Use each study's recorded parser
version when replaying it.

Keep each fact tied to its own evidence: approval, market reference adjustment,
aggregate feed share-count updates, legal registration, holder entitlement,
holder credit, first sellability, cash-dividend entitlement, actual payment and
spendable cash. One date or aggregate amount cannot fill the other fields.
Retain reversals and conflicting source claims in the order observed.

The arithmetic capital ratio is not a holder ledger. Verify eligible old quantity,
fractional entitlements, pending/credited/sellable lots, subscription payments and
cash receipts only to the extent needed by the declared scenario. It is not
necessary for all facts to be printed in one document if the source chain reliably
links issuer, event and instrument. A combined event requires separate components.

For reference discrepancies, separate the unrounded formula, the historical order
tick and the exchange's corporate-action rounding rule. A plausible tick-size
explanation is a hypothesis until the applicable rule is supported. Do not relax
the strict ledger guard simply to make one event pass.

For a blocked event, first read the existing case's sources and missing fields.
Do not start a fresh generation on every repeated review. Reopen it when a new
primary document or a concrete source lead can settle a necessary field. Keep the
affected financial path unknown; keep unrelated paths and source observations.
Any narrower research scope must be defined without looking at its future returns.

For example, a legal capital registration can be fully documented while the
bonus release date remains unavailable. The useful endpoint is a precise blocked
ledger decision, not an invented release date or a perpetual legal-document search.
