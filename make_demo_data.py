"""Creates data/baseline.csv (clean), data/batch_dirty.csv (quality + drift
problems) and data/batch_schema_change.csv (renamed/dropped columns)."""
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent / "data"

rng = np.random.default_rng(42)
CITIES = ["Hyderabad", "Mumbai", "Delhi", "Chennai", "Bengaluru"]


def make(n, start_id=1, purchase_scale=1.0):
    ids = np.arange(start_id, start_id + n)
    return pd.DataFrame({
        "customer_id": ids,
        "age": rng.integers(18, 70, n),
        "income": rng.lognormal(10.5, 0.4, n).round(2),
        "city": rng.choice(CITIES, n),
        "email": [f"user{i}@example.com" for i in ids],
        "purchase_amount": (rng.gamma(4, 250, n) * purchase_scale).round(2),
    })


base = make(2000)
DATA_DIR.mkdir(parents=True, exist_ok=True)
base.to_csv(DATA_DIR / "baseline.csv", index=False)

# ---- batch 1: dirty values + semantic drift (purchase_amount now in a different unit)
d = make(1000, start_id=5001, purchase_scale=2.2)
d["income"] = d["income"].astype(float)
d.loc[rng.choice(1000, 150, replace=False), "income"] = np.nan          # null spike
idx = rng.choice(1000, 30, replace=False)
d.loc[idx[:15], "age"] = -5                                             # impossible ages
d.loc[idx[15:25], "age"] = 200
d["age"] = d["age"].astype(object)
d.loc[idx[25:], "age"] = "twenty"                                       # type violation
d["city"] = d["city"].astype(object)
d.loc[rng.choice(1000, 60, replace=False), "city"] = " mumbai "         # fixable formatting
d.loc[rng.choice(1000, 20, replace=False), "city"] = "Unknown"          # invalid category
d.loc[rng.choice(1000, 25, replace=False), "email"] = "not-an-email"    # bad format
d = pd.concat([d, d.sample(40, random_state=1)])                        # duplicate rows
d.to_csv(DATA_DIR / "batch_dirty.csv", index=False)

# ---- batch 2: schema evolution (upstream team renamed income, dropped email, added phone)
s = make(1000, start_id=9001)
s = s.rename(columns={"income": "salary"}).drop(columns=["email"])
s["phone"] = rng.integers(7000000000, 9999999999, 1000)
s.to_csv(DATA_DIR / "batch_schema_change.csv", index=False)
print("demo data written to data/")
