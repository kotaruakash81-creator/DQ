import os
import runpy
import pandas as pd
import streamlit as st
from dq_engine import AuditLog, build_contract, profile, run_pipeline

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
BASELINE_PATH = os.path.join(DATA_DIR, "baseline.csv")

if not st.runtime.exists():
    raise SystemExit("Start this app with `streamlit run app.py` or run_app.bat.")

st.set_page_config(page_title="DQ-Guard", layout="wide")
st.title("DQ-Guard: Data Quality + Schema Evolution Engine")

CONSUMERS = {
    "revenue_dashboard": ["purchase_amount", "city"],
    "churn_model": ["age", "income", "purchase_amount"],
    "marketing_emails": ["email", "city"],
}

if not os.path.exists(BASELINE_PATH):
    runpy.run_path(os.path.join(APP_DIR, "make_demo_data.py"))

# ---------------------------------------------------------------- sidebar
st.sidebar.header("Data source")
mode = st.sidebar.radio("Incoming batch", ["Demo: dirty batch", "Demo: schema change", "Upload CSV"])
base_file = st.sidebar.file_uploader("Baseline CSV (optional)", type="csv")
base = pd.read_csv(base_file) if base_file else pd.read_csv(BASELINE_PATH)

if mode == "Demo: dirty batch":
    new = pd.read_csv(os.path.join(DATA_DIR, "batch_dirty.csv"))
elif mode == "Demo: schema change":
    new = pd.read_csv(os.path.join(DATA_DIR, "batch_schema_change.csv"))
else:
    up = st.sidebar.file_uploader("New batch CSV", type="csv")
    new = pd.read_csv(up) if up else None

if "audit" not in st.session_state:
    st.session_state.audit = AuditLog(os.path.join(APP_DIR, "audit_log.jsonl"))
if "contract" not in st.session_state:
    st.session_state.contract = build_contract(base, 1, CONSUMERS)
    st.session_state.audit.add("contract_created", {"version": 1})

audit, contract = st.session_state.audit, st.session_state.contract

if new is None:
    st.info("Upload a batch to check it.")
    st.stop()

if st.sidebar.button("Run pipeline", type="primary"):
    st.session_state.res = run_pipeline(base, new, contract, audit)
    st.session_state.res["batch_name"] = mode

res = st.session_state.get("res")
if not res:
    st.info("Pick a batch and press *Run pipeline* in the sidebar.")
    st.stop()

# ---------------------------------------------------------------- summary - FIXED PART
dec = res.get("decision", "PASS") if res else "PASS"
# Safe color mapping - eppudu KeyError radu
color_map = {"PASS": "green", "REPAIRED": "orange", "REPAIRED + DRIFT ALERT": "orange", "BLOCKED": "red"}
color = color_map.get(dec.upper() if isinstance(dec, str) else dec, "green")

st.markdown(f"### Decision: :{color}[{dec}]")
if not res:
    st.stop()
crit = sum(i["severity"] == "CRITICAL" for i in res.get("issues", []))
c1, c2, c3, c4 = st.columns(4)
c1.metric("Critical issues", crit)
c2.metric("Drifted columns", sum(d.get("drift", False) for d in res.get("drift", [])))
c3.metric("Consumers at risk", len(res.get("impact", {})))
c4.metric("Contract version", contract["version"])

tabs = st.tabs(["Issues", "Drift", "Schema & impact", "Repair", "Contract", "Audit log"])

with tabs[0]:
    st.dataframe(pd.DataFrame(res.get("issues", [])), use_container_width=True)
    st.subheader("Profile of incoming batch")
    st.json(profile(new))

with tabs[1]:
    d = pd.DataFrame(res.get("drift", []))
    st.dataframe(d, use_container_width=True)
    for row in res.get("drift", []):
        if row.get("drift") and row.get("kind") == "numeric":
            st.warning(f"Semantic drift in *{row['column']}*: mean moved "
                       f"{row['base_mean']} to {row['new_mean']} (PSI {row['psi']}).")
            chart = pd.DataFrame({"baseline": base[row["column"]].describe(),
                                  "new batch": pd.to_numeric(new[row["column"]], errors="coerce").describe()})
            st.bar_chart(chart.drop(index="count"))

with tabs[2]:
    sd = res.get("schema_diff", {})
    st.write("*Added:*", sd.get("added") or "none")
    st.write("*Removed:*", sd.get("removed") or "none")
    st.write("*Type changes:*", sd.get("dtype_changed") or "none")
    for old, newc in sd.get("likely_renames", []):
        st.success(f"Likely rename: {old} is now {newc} (same distribution)")
    st.subheader("Downstream impact")
    if res.get("impact"):
        for name, cs in res["impact"].items():
            st.error(f"*{name}* is affected through: {', '.join(cs)}")
    else:
        st.success("No downstream consumer affected.")

with tabs[3]:
    if res.get("cert") is None:
        st.error("Batch blocked: structural problem, so auto-repair is not safe.")
    else:
        st.write("*Repair actions*")
        for line in res.get("log") or ["nothing to repair"]:
            st.write("-", line)
        st.write("*Correctness certificate*")
        st.json(res.get("cert"))
        a, b = st.columns(2)
        a.write("Repaired data")
        a.dataframe(res.get("repaired").head(100))
        b.write("Quarantined rows")
        b.dataframe(res.get("quarantine").head(100))
        st.download_button("Download repaired CSV", res["repaired"].to_csv(index=False), "repaired.csv")

with tabs[4]:
    st.json(contract)
    if res.get("cert") is not None and st.button("Accept batch schema: publish next contract version"):
        st.session_state.contract = build_contract(res["repaired"], contract["version"] + 1, CONSUMERS)
        audit.add("contract_evolved", {"from": contract["version"], "to": contract["version"] + 1})
        st.rerun()

with tabs[5]:
    ok = audit.verify()
    (st.success if ok else st.error)(
        "Hash chain intact: no record has been altered." if ok else "TAMPERING DETECTED in audit log.")
    st.dataframe(pd.DataFrame(audit.records)[["ts", "event", "hash"]], use_container_width=True)