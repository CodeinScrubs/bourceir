---
name: brs-evidence-check
description: Verify BRS review-package hashes and interpret validation receipts with correct scope; use for audit ZIPs, evidence handoffs and release checks, not as proof of source authenticity or profitability.
---

# BRS evidence check

Read `AGENTS.md`. Run commands from the repository root with its local Python.

## Package integrity

For a review ZIP whose root manifest is a flat mapping of entry names to SHA-256 strings:

```powershell
.\venv\Scripts\python.exe .agents/skills/brs-evidence-check/scripts/verify_bundle.py "<bundle.zip>"
```

Use `--require-complete` when every payload file must be covered. The manifest itself is excluded from self-hashing. Unhashed cover notes remain a coverage gap even when CRC and all listed hashes pass. The helper reads without extracting or executing content; it does not validate the meaning or authenticity of documents.

For structured research/study manifests, use the study's existing reviewer/replayer. Do not reinterpret an unsupported manifest shape as a flat hash list. Use `ops.research_bundle` for a market-only export; the helper does not redact private input packages.

For a new handoff, finalize the cover note before calculating the inventory so it is hashed too. Preserve earlier packages. Use a fresh output only when there is a useful new deliverable, not merely because a review repeats known facts.

When handing off project skills for review, exclude `__pycache__` and compiled bytecode. Include the repository AGENTS routing and the actual helper-test receipt if the handoff claims those were checked; bind the receipt to the helper/test source hashes and keep its original execution time.

## Test and evidence scope

Bind receipts to the actual selected tests, completion/exit status, code/data versions and time. Separate structural integrity, reproducibility, source correctness, economic validity, prospective observations and broker fills.

Run the smallest meaningful checks for the change, while honoring mandatory AGENTS/CI pre-commit and release gates. For skills/documentation, validate the skill structure, links and any helper behavior. A prior full application run may be cited with its original scope; do not relabel its counts as a fresh run. Runtime changes, unresolved regressions or a required release gate can justify a new full run. Use `python -m ops.validation_run --help` for the existing receipt-producing offline runner; do not repeatedly rewrite ad-hoc dot counters or rerun an hour-long suite for wording changes.

Keep private test receipts under `data/private/`; never claim a skipped test, incomplete run, prior receipt or paper simulation proves live performance. Finish once the applicable checks pass unless new evidence justifies another check.
