# Native daily observations and feature inputs

Repository entry points: `data_collector/tsetmc.py`, `core/daily_publications.py`,
`database/symbol_review.py`, `features/store.py`, `features/provenance.py`.
For current commands and receipt semantics read
[daily publications](../../../../docs/daily-publications-fa.md).

1. Pin instrument identity, session range and requested fields. Distinguish today's
   active registry from historical membership. Preserve the requested denominator.
2. Fetch through the existing provider/collector. Reconcile session identity, raw
   OHLC, official closing, reference, volume, value and trade count. Validate before
   any approved database correction; preserve previous generations and use the
   existing migration/backup and publication paths.
3. Preserve separate statuses for a valid trade day, reported zero activity,
   unavailable response, invalid row and unknown session coverage. No missing row
   becomes a zero-return blocked entry merely because there is no candle.
4. A daily candle or even a dated upper price limit does not prove an order's queue
   position or fill. Intraday event ordering requires dated, ordered observations;
   do not infer low-then-high from an OHLC bar.
5. After raw inputs change, use `features.provenance.assert_compatible_inputs`
   and the pinned `features.store.load_feature_snapshot` path before research.
   Reject stale inputs rather than pairing a new raw table with old features.

Archive only the bounded evidence needed to reproduce this correction. Hash and
source consistency are useful findings; independent correctness and full market
coverage require their own evidence. Stop once the relevant correction or explicit
unavailable status is established.
