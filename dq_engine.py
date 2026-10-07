"""DQ-Guard core engine: profiling, data contract, validation, drift,
schema evolution, auto-repair with certificate, tamper-evident audit log."""
import hashlib
import json
import time

import numpy as np
import pandas as pd
from scipy import stats

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


def _is_num(dtype_str):
    return str(dtype_str).startswith(("int", "float"))


# ---------------------------------------------------------------- profiling
def profile(df):
    cols = {}
    for c in df.columns:
        s = df[c]
        p = {"dtype": str(s.dtype), "null_pct": round(float(s.isna().mean()), 4),
             "unique": int(s.nunique())}
        if _is_num(s.dtype) and s.notna().any():
            p.update(min=float(s.min()), max=float(s.max()),
                     mean=round(float(s.mean()), 3), std=round(float(s.std()), 3))
        cols[c] = p
    return {"rows": len(df), "duplicate_rows": int(df.duplicated().sum()), "columns": cols}


# ----------------------------------------------------------- data contract
def build_contract(df, version=1, consumers=None):
    cols = {}
    for c in df.columns:
        s = df[c]
        dt = str(s.dtype)
        rule = {"dtype": dt, "max_null_pct": round(float(s.isna().mean()) + 0.02, 4)}
        is_key = c.lower().endswith("id") and s.is_unique
        if _is_num(dt) and not is_key:
            lo, hi = float(s.min()), float(s.max())
            pad = 0.1 * (hi - lo)
            rule.update(min=lo - pad, max=hi + pad)
        elif not _is_num(dt):
            if s.nunique() <= 20:
                rule["allowed"] = sorted(s.dropna().astype(str).unique().tolist())
            if "email" in c.lower():
                rule["regex"] = EMAIL_RE
        if is_key:
            rule["unique"] = True
        cols[c] = rule
    return {"version": version, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "columns": cols, "consumers": consumers or {}}


# -------------------------------------------------------------- validation
def validate(df, contract):
    issues = []

    def add(sev, col, kind, detail, rows=0):
        issues.append({"severity": sev, "column": col, "type": kind,
                       "detail": detail, "rows": int(rows)})

    cols = contract["columns"]
    for c in cols:
        if c not in df.columns:
            add("CRITICAL", c, "missing_column", "Column required by contract is absent")
    for c in df.columns:
        if c not in cols:
            add("WARN", c, "new_column", "Column is not in the contract")

    for c, r in cols.items():
        if c not in df.columns:
            continue
        s = df[c]
        nullp = s.isna().mean()
        if nullp > r["max_null_pct"]:
            add("CRITICAL", c, "null_spike",
                f"null rate {nullp:.1%} exceeds allowed {r['max_null_pct']:.1%}", s.isna().sum())
        if _is_num(r["dtype"]):
            num = pd.to_numeric(s, errors="coerce")
            bad = num.isna() & s.notna()
            if bad.any():
                add("CRITICAL", c, "type_violation", "non-numeric values in numeric column", bad.sum())
            oor = (num < r["min"]) | (num > r["max"]) if "min" in r else num.isna() & False
            if oor.any():
                add("CRITICAL", c, "out_of_range",
                    f"values outside [{r['min']:.1f}, {r['max']:.1f}]", oor.sum())
        else:
            if "allowed" in r:
                bad = s.notna() & ~s.astype(str).isin(r["allowed"])
                if bad.any():
                    add("CRITICAL", c, "invalid_category", "values not in allowed set", bad.sum())
            if "regex" in r:
                bad = s.notna() & ~s.astype(str).str.match(r["regex"])
                if bad.any():
                    add("CRITICAL", c, "format_violation", "values fail the format rule", bad.sum())
        if r.get("unique") and s.duplicated().any():
            add("CRITICAL", c, "duplicate_key", "key column has duplicates", s.duplicated().sum())

    d = df.duplicated().sum()
    if d:
        add("WARN", "*", "duplicate_rows", "fully duplicated rows", d)
    return issues


# ------------------------------------------------------------------- drift
def psi(base, new, bins=10):
    qs = np.unique(np.quantile(base, np.linspace(0, 1, bins + 1)))
    if len(qs) < 3:
        return 0.0
    qs[0], qs[-1] = -np.inf, np.inf
    b = np.histogram(base, qs)[0] / len(base)
    n = np.histogram(new, qs)[0] / len(new)
    b, n = np.clip(b, 1e-4, None), np.clip(n, 1e-4, None)
    return float(np.sum((n - b) * np.log(n / b)))


def detect_drift(base, new):
    """Statistical (semantic) drift: KS + PSI for numbers, TVD for categories."""
    res = []
    for c in base.columns:
        if c not in new.columns:
            continue
        b, n = base[c].dropna(), new[c].dropna()
        if c.lower().endswith("id") or (not _is_num(base[c].dtype) and b.nunique() > 20):
            continue                      # keys / free text: drift is meaningless
        if _is_num(base[c].dtype):
            n = pd.to_numeric(n, errors="coerce").dropna()
            if len(n) < 20:
                continue
            ks = stats.ks_2samp(b, n)
            p = psi(b.values, n.values)
            res.append({"column": c, "kind": "numeric", "ks_stat": round(float(ks.statistic), 3),
                        "psi": round(p, 3), "base_mean": round(float(b.mean()), 2),
                        "new_mean": round(float(n.mean()), 2),
                        "drift": bool(ks.pvalue < 0.01 and p > 0.1)})
        else:
            bf = b.astype(str).str.strip().str.lower().value_counts(normalize=True)
            nf = n.astype(str).str.strip().str.lower().value_counts(normalize=True)
            idx = bf.index.union(nf.index)
            tvd = 0.5 * (bf.reindex(idx, fill_value=0) - nf.reindex(idx, fill_value=0)).abs().sum()
            res.append({"column": c, "kind": "categorical", "tvd": round(float(tvd), 3),
                        "drift": bool(tvd > 0.15)})
    return res


# --------------------------------------------------------- schema evolution
def schema_diff(contract, base, new):
    cols = contract["columns"]
    added = [c for c in new.columns if c not in cols]
    removed = [c for c in cols if c not in new.columns]
    changed = {c: (cols[c]["dtype"], str(new[c].dtype)) for c in cols
               if c in new.columns and cols[c]["dtype"] != str(new[c].dtype)}
    renames = []
    for r in removed:          # a removed + an added column with same distribution = rename
        for a in added:
            if _is_num(base[r].dtype) and _is_num(new[a].dtype):
                if stats.ks_2samp(base[r].dropna(), new[a].dropna()).statistic < 0.1:
                    renames.append((r, a))
            elif not _is_num(base[r].dtype) and not _is_num(new[a].dtype):
                if set(base[r].dropna().astype(str)) == set(new[a].dropna().astype(str)):
                    renames.append((r, a))
    return {"added": added, "removed": removed, "dtype_changed": changed, "likely_renames": renames}


def impact(contract, affected_cols):
    """Which downstream consumers touch an affected column?"""
    aff = set(affected_cols)
    return {name: sorted(set(cs) & aff) for name, cs in contract.get("consumers", {}).items()
            if set(cs) & aff}


# ------------------------------------------------------------------ repair
def repair(df, contract, base):
    df = df.copy()
    cols, log, rows_in = contract["columns"], [], len(df)

    for c, r in cols.items():                       # 1. coerce / normalise
        if c not in df.columns:
            continue
        if _is_num(r["dtype"]):
            before = df[c].isna().sum()
            df[c] = pd.to_numeric(df[c], errors="coerce")
            n = df[c].isna().sum() - before
            if n:
                log.append(f"{c}: {n} non-numeric values set to null")
        elif "allowed" in r:
            lookup = {a.lower(): a for a in r["allowed"]}
            raw = df[c].astype(str)
            fixed = raw.str.strip().str.lower().map(lookup)
            ch = fixed.notna() & (raw != fixed) & df[c].notna()
            if ch.any():
                df.loc[ch, c] = fixed[ch]
                log.append(f"{c}: {int(ch.sum())} values normalised (case/whitespace)")

    bad = pd.Series(False, index=df.index)          # 2. quarantine invalid rows
    for c, r in cols.items():
        if c not in df.columns:
            continue
        if _is_num(r["dtype"]):
            if "min" in r:
                bad |= (df[c] < r["min"]) | (df[c] > r["max"])
        else:
            if "allowed" in r:
                bad |= df[c].notna() & ~df[c].astype(str).isin(r["allowed"])
            if "regex" in r:
                bad |= df[c].notna() & ~df[c].astype(str).str.match(r["regex"])
    quarantine = df[bad]
    df = df[~bad]
    if len(quarantine):
        log.append(f"quarantined {len(quarantine)} rows that violate range/category/format rules")

    for c, r in cols.items():                       # 3. impute nulls from baseline
        if c in df.columns and df[c].isna().any() and c in base.columns:
            n = int(df[c].isna().sum())
            if _is_num(r["dtype"]):
                df[c] = df[c].fillna(base[c].median())
                log.append(f"{c}: {n} nulls imputed with baseline median")
            elif base[c].notna().any():
                df[c] = df[c].fillna(base[c].mode()[0])
                log.append(f"{c}: {n} nulls imputed with baseline mode")

    d0 = len(df)                                    # 4. de-duplicate
    df = df.drop_duplicates()
    for c, r in cols.items():
        if r.get("unique") and c in df.columns:
            df = df.drop_duplicates(subset=[c])
    if d0 - len(df):
        log.append(f"removed {d0 - len(df)} duplicate rows")

    for c, r in cols.items():                       # 5. restore int dtypes
        if c in df.columns and str(r["dtype"]).startswith("int") and df[c].notna().all():
            df[c] = df[c].round().astype(r["dtype"])

    remaining = [i for i in validate(df, contract) if i["severity"] == "CRITICAL"]
    cert = {"passed": not remaining, "remaining_critical_issues": len(remaining),
            "rows_in": rows_in, "rows_out": len(df), "rows_quarantined": len(quarantine),
            "contract_version": contract["version"],
            "sha256": hashlib.sha256(df.to_csv(index=False).encode()).hexdigest()}
    return df, quarantine, log, cert


# ------------------------------------------------------ tamper-evident log
class AuditLog:
    """Hash-chained log: editing any past record breaks every later hash."""

    def __init__(self, path="audit_log.jsonl"):
        self.path, self.records = path, []
        try:
            with open(path) as f:
                self.records = [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            pass

    @staticmethod
    def _h(rec):
        body = {k: v for k, v in rec.items() if k != "hash"}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def add(self, event, payload):
        prev = self.records[-1]["hash"] if self.records else "0" * 64
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event,
               "payload": payload, "prev": prev}
        rec["hash"] = self._h(rec)
        self.records.append(rec)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")

    def verify(self):
        prev = "0" * 64
        for rec in self.records:
            if rec["prev"] != prev or rec["hash"] != self._h(rec):
                return False
            prev = rec["hash"]
        return True


# ---------------------------------------------------------------- pipeline
def run_pipeline(base, new, contract, audit):
    issues = validate(new, contract)
    drift = detect_drift(base, new)
    sd = schema_diff(contract, base, new)
    drifted = [d["column"] for d in drift if d["drift"]]
    affected = {i["column"] for i in issues if i["severity"] == "CRITICAL"} | set(drifted) \
        | set(sd["removed"]) | set(sd["dtype_changed"])
    result = {"issues": issues, "drift": drift, "schema_diff": sd,
              "impact": impact(contract, affected)}

    if any(i["type"] == "missing_column" for i in issues):
        result["decision"] = "BLOCKED"
        result["repaired"], result["quarantine"], result["log"], result["cert"] = None, None, [], None
    else:
        rep, quar, log, cert = repair(new, contract, base)
        result.update(repaired=rep, quarantine=quar, log=log, cert=cert)
        if not cert["passed"]:
            result["decision"] = "BLOCKED"
        elif drifted:
            result["decision"] = "REPAIRED + DRIFT ALERT"
        elif issues:
            result["decision"] = "REPAIRED"
        else:
            result["decision"] = "PASS"

    audit.add("batch_checked", {
        "decision": result["decision"], "critical": sum(i["severity"] == "CRITICAL" for i in issues),
        "drifted_columns": drifted, "schema": sd,
        "certificate": result["cert"], "contract_version": contract["version"]})
    return result
