# DQ-Guard: Data Quality + Schema Evolution Engine (Hacksphere 2026, Problem 10)

## Run
    pip install -r requirements.txt
    python make_demo_data.py
    streamlit run app.py

In the sidebar pick a demo batch and press **Run pipeline**.

## What it does
| Deliverable | Where |
|---|---|
| Continuous profiling + anomaly detection | `profile()`, `validate()`, `detect_drift()` (KS test, PSI, TVD) |
| Schema evolution detection + impact analysis | `schema_diff()` (adds, removes, type changes, likely renames), `impact()` maps issues to downstream consumers |
| Automated repair with certificates | `repair()` returns repaired data, quarantined rows, action log and a certificate (re-validation result + SHA-256 + row counts) |
| Data contract with consumer safety | `build_contract()` creates a versioned contract; batches that break it are repaired or BLOCKED; a tamper-evident hash-chained audit log records every decision |

## Demo script (2 min)
1. **Dirty batch**: nulls, impossible ages, text in numeric column, bad emails, duplicates are caught and repaired.
   `purchase_amount` is *valid* but drifted (unit change): only the drift detector catches it.
2. **Schema change**: `income` renamed to `salary`, `email` dropped. Batch is BLOCKED, rename detected, impacted consumers listed.
3. **Evolve contract** to v2, then show the **audit log** hash chain.

## Honest limits (say this to the judges)
The problem asks for "zero false negatives, forever". That is impossible in general:
- Whether arbitrary downstream code "fails silently" is an undecidable semantic property (Rice's theorem).
- Any statistical drift test trades off false positives vs false negatives; with finite samples a small drift can be missed.
- A "certificate" here means: the repaired batch passes every rule in the contract (checkable by anyone, SHA-256 pinned). It cannot cover rules nobody wrote.

What we guarantee instead: **no failure that violates the contract can pass silently.** Every violation is detected, repaired or quarantined, logged in a tamper-evident chain, and mapped to the consumers it affects.

## Ideas if you have extra time
- Add Great Expectations / pandera export of the contract
- Per-consumer contracts (each consumer declares the columns and rules it needs)
- Rolling-window drift (compare against last N batches, not only the baseline)
