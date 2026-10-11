# External review response: AI audit + PR #9 (2026-10-12)

> **خلاصه فارسی برای کاربر:** ادعاها و پیشنهادهای هوش مصنوعی دیگر و کد PR شماره ۹ آن، یک‌به‌یک روی کد و
> **داده واقعی** (۱.۵۵ میلیون کندل، ۵۹۴ نماد) بررسی شد. بیشتر ادعاهای «باگ» مربوط به نسخه قدیمی 0.4.2 بود
> که قبلاً رفع شده است. چند ادعا اشتباه بود (مثلاً دامنه نوسان ±۵٪ و کارمزد ۰.۵٪). کد PR ۹ چند ایده خوب داشت
> که منتقل شد، ولی ادغام مستقیم آن سامانه را از کار می‌انداخت (حجم دیتابیس دو برابر، انبار فیچر خالی،
> ~۱۸۹ هزار درخواست اضافه در هر به‌روزرسانی روزانه، توقف بکتست روی داده واقعی). در همین بازبینی دو باگ
> مهم و واقعی هم پیدا و رفع شد که هیچ‌کدام از دو بازبینی قبلی ندیده بودند: **تشخیص صف قفل** و **اقدام شرکتی در
> بکتست**. نتیجه: نسخه 0.5.1 روی شاخه `feature/foundation-audit-fixes` (۱۱۱ آزمون آفلاین).

Audience: the other AI reviewer and any future contributor. Everything below was measured, not assumed.

## 1. Method
- Baseline: `develop` @ `168da04` (v0.5.0, PRs #7 and #8 merged, CI green). Not the v0.4.2 snapshot the audit describes.
- Real data: `data/brs.db` with 1,550,478 daily candles (2001-03-25 to 2026-10-11), 1,364,356 client-type rows, 594 TSE/IFB equities.
- PR #9 (`f051066`, branch `feature/brs-foundation-review`) was checked out in an isolated worktree on Windows,
  run against a **copy** of the real DB and the live TSETMC feed. Its own 124 tests pass; the real-data results are below.
- Secret scan: all 13 commits on every branch were searched for the TypeSafe key. 0 hits; `.env` was never committed.

## 2. Verdicts on the audit's claims

| Claim | Verdict | Evidence |
| :--- | :--- | :--- |
| `adj_close` and `close` are misinterpreted; rename the fields | Already fixed in v0.5.0 | migration 005: `close_raw` (last trade), `close_final_raw` (official close), `ref_price_raw`; `adj_close` is display-only (AGENTS rule 10) |
| `captured_at` is never set, so every snapshot is rejected | Already fixed | `test_acceptance_provider_snapshot_passes_validation` |
| Duplicate symbols; use insCode as key | Already fixed | migration 005 merges duplicates; `UNIQUE` index on `ins_code` and `symbol_fa` |
| Backtest fills on the same day and at impossible prices | Already fixed | t+1 VWAP clipped to [low, high]: `test_replay_executes_on_next_day_without_lookahead`, `test_fill_price_never_outside_day_range` |
| Equity curve uses cost instead of market value | Already fixed | `test_final_equity_is_mark_to_market` |
| Migrations never run; ORM `models.py` | Already fixed | auto-migrator with backups; `models.py` deleted |
| Uses `pytse_client`; use its bulk order-book endpoint | Outdated | direct CDN provider; one `GetMarketWatch` call returns 5-level depth for all symbols |
| "22 tests"; "Python 3.14" | Outdated | 88 tests at review start, 111 now; venv and CI on 3.11 |
| `experiments` table does not exist | Wrong | exists since migration 002 |
| Daily price limit is ±5% | **Wrong** | the band changed many times; see §5 (currently ±3%; +6%/−2% in 2021; ±2% in mid-2024) |
| Fees ≈ 0.5% both sides + 0.02% tax | **Wrong** | user's broker: buy 0.3712%, sell 0.88% → 1.26% round-trip break-even (`config/default.yaml`) |
| Doctor should check the Python version | Already done | now raised from WARN to ERROR |
| Feature runs need metadata | Partly valid | manifest existed; provenance (git commit, code/lock hashes, schema version, fees, parquet SHA-256) and atomic writes added |
| Prediction ledger, experiment registry, regime engine | Valid, not yet due | scheduled in roadmap 1c/1f (`docs/phase1_design.md` §6). A ledger with no producer would be an untested schema |
| Move to PostgreSQL/TimescaleDB, Airflow, Kafka | Disagree for now | single user on one laptop; SQLite WAL handles 1.5M rows easily. Revisit if snapshot storage outgrows it |

## 3. PR #9 (`f051066`): adopted vs rejected

### Measured on the real DB (Windows, copy of `brs.db`)
| PR #9 change | Real-data result |
| :--- | :--- |
| Migration 007: archive every daily/client-type row into `*_revisions`, mark all rows `unverified` | 27 s; DB 498 MB → **1,059 MB** (2.9 M duplicated rows) |
| Feature build reads only "verified" rows | **Fails**: "no verified raw data", so no features until a full re-download |
| `compute_daily_features` raises on any non-positive ref/close row | **101 of 594 symbols** raise (first archive day has `priceYesterday = 0`) and the whole build aborts |
| Client-type refresh re-requests every date lacking client types | **188,937 extra HTTP requests per daily update** (≈26 h at the 0.5 s rate limit). Pre-2015 dates have no client-type data on TSETMC, so this repeats every day |
| Backtest refuses any invalid ref/VWAP row and any held corporate action | فولاد / خودرو / شپنا since 2023 all **raise** (one NULL `ref_price_raw` on 2023-03-27); 8,240 ex-dates in the universe |
| `upsert_symbol` returns None when the insCode changes | Stops updating symbols that move between markets (they get a new insCode) |
| SETUP.bat rewrite | English-only, requires `uv` with no fallback, and runs ruff + the full test suite on the user's machine. Unsuitable for a non-English-speaking, non-programmer user |
| CI workflow | First run failed in 0 s ("workflow file issue"); fixed later in `70104ff`, after which 124 tests passed on Windows and Linux (verified: runs 38178722113, 38178724521, 38178937157) |

Its premise that history should be re-fetched to be "verified" adds nothing: the archive *is* TSETMC's current
archive either way. Bitemporal "as-first-published" data cannot be recovered retroactively; it only starts with
live snapshots. Both points are recorded as `known_limitations` in the feature manifest.

### Adopted (re-implemented on top of `develop`, with tests)
| Idea | Where |
| :--- | :--- |
| Provider timestamp from `dEven + hEven`, never guessed (live feed verified to include `dEven`) | `data_collector/tsetmc.py` |
| `captured_at` taken after both responses arrive | `tsetmc.get_market_snapshots` |
| Snapshot change detection on **all** fields (depth levels 2–5 were ignored) | `scheduler._fingerprint` |
| Unknown is unknown: no fake buyer power 1.0, freshness "Unknown" without server time, no made-up quality scores, profit factor `None` instead of 99 | `monitor.py`, `db.py`, `engine.py` |
| RSI of a flat series = 50; market flow ratio uses a matched numerator/denominator; honest `days_since_listing` label | `features/` → `daily-v2` |
| Experiments immutable (`INSERT`, not `INSERT OR REPLACE`) | `db.log_experiment` |
| Atomic, transactional migrations + checksums of applied files | `database/migrator.py` |
| Fail fast on broken or dangerous config (fees, the 5% participation cap, Rial/Tehran) | `config/settings.py` |
| Drawdown measured from initial capital; `net_pnl` = equity change; strategy never sees `adj_close` | `backtest/engine.py` |
| Job identity by process create-time (PID reuse); log tail reads the last 64 KB | `ops/jobs.py` |
| Tehran-time log timestamps; live loop compensates for request time; `doctor --json`; FK check; psutil/pyarrow checks | `brs.py`, `ops/doctor.py` |
| Windows + Linux CI matrix, read-only permissions, `pip check`, offline doctor | `.github/workflows/tests.yml` (standard action tags) |
| START.bat stops if SETUP failed | `START.bat` |

## 4. New defects found during this review (neither audit found these)

### 4.1 Limit-lock detection missed more than half of all lock days (P0)
`lock_flags` compared the **official close** with the reference price. When daily volume is below the base volume
(حجم مبنا), the official close moves only partially (`ref + (price − ref) × volume / base_volume`). A low-volume
day locked at the limit therefore looked like a +0.5% day.

| | v0.5.0 (official close ≥ 1.5%) | v0.5.1 (traded price ≥ 0.5%) |
| :--- | ---: | ---: |
| Locked-up days (all history) | 67,486 | 106,125 |
| Locked-down days (all history) | 26,566 | **100,212** |
| Locked-down days in 2021 | 1,693 | 19,583 |

Impact: the backtest could *sell inside a locked sell queue*, which is exactly the impossible fill the project
exists to prevent. Lock features were mostly wrong in crash periods. The 0.5% threshold sits far below the
smallest band ever observed (2%). A band-relative rule (cross-sectional band per day) was also evaluated: it
disagrees on 3% of single-price days, all before 2019, and the 0.5% rule is the conservative superset.

### 4.2 Corporate actions were booked as trading losses in the backtest (P0)
On an ex-date (dividend, bonus shares, rights), the position was marked at the adjusted price with no compensation.

| Buy & hold since 2022-01 | v0.5.0 engine | v0.5.1 engine | PIT total return |
| :--- | ---: | ---: | ---: |
| فولاد | −60.6% | +313.8% | +315.6% |
| وبملت | −52.7% | +812.6% | +817.1% |
| کگل | −85.4% | +17.3% | +18.4% |

Fix: value-preserving share adjustment (`quantity × prev_close / ref`), the same total-return convention as
`pit_daily_returns`, so labels and backtests agree. The remaining gap (< 1%) is fees, slippage and VWAP vs close.
A permanent acceptance test checks this reconciliation. A first attempt (fixed-value receivable) was rejected
because it ignores how bonus shares grow after the ex-date. Documented simplification: delays in dividend payment
and bonus-share registration are not modelled; the number of crossings is reported in the metrics.

### 4.3 Smaller defects
Live snapshots ignored depth changes at levels 2–5. The provider timestamp guessed "today or yesterday" (wrong after
multi-day holidays). Same-day client-type data was assumed to be partial: verified on live data that published
values are final, so no refresh is needed.

## 5. Price-band regimes observed in the data
Median single-price (locked) move per month, TSE/IFB equities:

| Period | Band |
| :--- | :--- |
| 2015 – 2020 | ±5% |
| Feb – Apr 2021 | +6% / −2% (asymmetric) |
| 2022 – early 2024 | ±5% (TSE); up to ±7% for part of the universe |
| Jun and Aug 2024 | ±2% |
| Sep 2024 – now | ±3% |

Hard-coding any band (as the audit's "±5% limit" test suggests) would be wrong for most of the history.

## 6. Still open (tracked in `docs/phase1_design.md`)
- 1b part 2: Codal history backfill and event features.
- 1c: fee-aware labels on **executable** prices (next-day VWAP, not the close); benchmarks (random entry,
  equal-weight universe; official TEDPIX / equal-weight index as data); pessimistic-execution stress test
  (buy at high, sell at low); regime features; every run logged immutably in `experiments` with git commit and
  the feature parquet hash.
- 1d: LightGBM, purged/embargoed walk-forward.
- 1f: immutable prediction ledger plus ≥ 8 weeks of forward (paper) testing before any reliance.
- Survivorship: delisted symbols are absent from the universe (documented in the manifest).

## 7. Working agreement for future contributions
1. Branch from the latest `develop`, not an old snapshot. Read `AGENTS.md`, `CLAUDE.md` and `docs/phase1_design.md` first.
2. Any data-layer change must be run against real data (the user has the DB and a working TSETMC route), not only synthetic tests.
3. Never invalidate existing data wholesale or force a full re-download without measuring the cost.
4. "Unknown" is `None`/NaN. Never use a default number or raise for the whole run because of one bad row.
5. The user is a Persian-speaking non-programmer: setup and UI messages stay Persian and simple.

## 8. Round 2: updated PR #9 and three further AI reviews (verified 2026-10-12)

### 8.1 PR #9 after its update
- New commits `70104ff` (CI env context) and `d726144` (docs only). CI is now green on Windows and Linux; I verified all three
  cited run IDs on GitHub. **No Python file changed**, so every real-data result in §3 still holds: DB doubled,
  feature build blocked, ~189k extra requests per update, backtest raising on real symbols, English-only setup.
- Its install guide tells the user to clone PR #9's branch into `C:\BRS\review` and send `doctor-feed.json`. That isn't
  needed: the user's existing install already passes the doctor against the live feed (TSETMC 0.8 s, clock skew 2 s,
  Codal and Jev OK). `brs.py doctor --json logs/doctor.json` now produces the same kind of machine-readable report.

### 8.2 Ideas from PR #9 that were right and are now adopted
| Idea | Verification | Implementation |
| :--- | :--- | :--- |
| Re-request client-type dates that are missing | Right in principle: **~360 real holes** (2020 and 2022; e.g. 143 symbols missing 2022-02-08). TSETMC's *full-history* endpoint dropped them and its *per-date* endpoint has them. Wrong in scope: dates before a symbol's first record have no data on TSETMC, so requesting them daily costs 189k requests for nothing | `scheduler._missing_client_type_dates`: new days + at most 20 holes per symbol per run, only between the symbol's first and last recorded dates and only on dates for which TSETMC published client types for ≥ 20 other symbols. That excludes 8,789 symbol-days in 2009 where the per-date endpoint returns HTTP 500 (found while running the real update). One failing date no longer drops the symbol's other dates. Next update: 781 requests in total |
| Keep values that the source later revises | Valuable for data-quality auditing | migration `007_revision_log.sql`: triggers store the *previous* version only when a value actually changes. No baseline copy, no "unverified" gate. DB size is essentially unchanged |
| A multi-day exit counts as one trade | Right: win rate was counted per partial fill | round-trip aggregation in `BacktestEngine` |
| Report real inserts, not attempts | Right (`INSERT OR IGNORE`) | `insert_snapshots_bulk` returns `rowcount` |
| No fill without liquidity information | Right for zero-volume days | the replay skips execution on days without trades |

### 8.3 The three external reviews: accepted points and hallucination check
Accepted and scheduled (see `docs/phase1_design.md` §6):
- historical universe membership (survivorship bias) in 1c;
- a multi-symbol portfolio simulator in 1c;
- benchmarks (random entry, equal-weight universe, TEDPIX as data) in 1c;
- purged/embargoed walk-forward in 1d;
- feed-reliability report (cycles, gaps over 90 s, server-to-capture delay; pilot gate of 5 sessions with ≥95% cycles) in 1e;
- queue-position and fill-probability execution model in 1e;
- the LLM explains and never decides (already AGENTS rule 8).

Done now: a wealth-neutrality acceptance test for every corporate-action type (cash dividend, bonus issue, rights issue, consolidation).

Claims in those reviews that do not hold up:
- They score PR #9 as "Data engineering 9.5/10, Reproducibility 9.5/10" from its description alone. None of them ran it on data; on the real DB it blocks feature generation (§3).
- They say "the current data foundation is mostly daily". In fact 30-second, five-level snapshots of the whole market already exist (the reviewer later corrected this itself).
- They say immutable feature runs require copying all inputs per run. Reproducibility is achieved with the manifest provenance (git commit, code/lock hashes, schema version, parquet SHA-256) plus the revision log. Pinning a dataset copy belongs to the experiment runner in 1c, so only experiments that matter cost disk.
- They suggest a fixed `price_band(ticker, date)` table is required. The lock rule does not need the band (§4.1). A per-date band table is only needed for intraday queue modelling (1e) and can be derived from the data (§5).
