"""Control room. Run: streamlit run dashboard/app.py"""
import json
import os
import sys
from pathlib import Path

import requests
import streamlit as st

API = os.getenv("API_URL", "http://localhost:8000")
st.set_page_config(page_title="Exception Resolver", layout="wide")


def call(method, path, **kw):
    try:
        r = requests.request(method, API + path, timeout=60, **kw)
        return r.json() if r.content else {}
    except Exception as e:
        st.error(f"API error: {e}")
        return {}


COLORS = {"OPEN": "🔵", "WAITING": "🟡", "AWAITING_APPROVAL": "🟠", "ESCALATED": "🔴", "CLOSED": "🟢"}

with st.sidebar:
    st.header("Demo controls")
    h = call("GET", "/health")
    st.caption(f"Mode: **{h.get('mode')}** · Today: **{h.get('today')}**")
    cust = st.radio("Customer profile", ["X", "Y"], horizontal=True,
                    help="X = ERP with API (NATIVE). Y = Tally/Excel/emailed PDF (FALLBACK).")
    if st.button("Scan ERP / files now (WF2)"):
        st.success(call("POST", f"/scan/{cust}"))
    d = st.date_input("Simulated today")
    if st.button("Set clock"):
        call("POST", "/admin/clock", params={"today": str(d)})
    st.divider()
    po = st.text_input("PO for GRN update", "PO-1001")
    q = st.number_input("Qty received", 1, 1000, 10)
    if st.button("Warehouse records GRN"):
        st.success(call("POST", "/admin/grn", params={"customer": cust, "po_number": po, "qty": q}))
    st.divider()
    cap = st.selectbox("Capability", ["grn", "po", "invoice"])
    c1, c2 = st.columns(2)
    if c1.button("Break native"):
        call("POST", "/admin/health", json={"customer": cust, "capability": cap, "healthy": False})
    if c2.button("Restore"):
        call("POST", "/admin/health", json={"customer": cust, "capability": cap, "healthy": True})
    if st.button("Reset demo", type="secondary"):
        call("POST", "/admin/reset")

tab_cases, tab_paths, tab_metrics = st.tabs(["Cases", "Capability router", "Evaluation"])

with tab_cases:
    cases = call("GET", "/cases") or []
    if not cases:
        st.info("No cases yet. Click 'Scan ERP / files now'.")
    left, right = st.columns([2, 3])
    with left:
        for c in cases:
            label = f"{COLORS.get(c['status'], '')} {c['id']} · {c['po_number']} · {c['case_class']} · {c['status']}"
            if st.button(label, key=c["id"], use_container_width=True):
                st.session_state["sel"] = c["id"]
    sel = st.session_state.get("sel")
    if sel:
        with right:
            c = call("GET", f"/cases/{sel}")
            st.subheader(f"{c['id']} — {c['po_number']} ({c['customer']})")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("Class", c["case_class"])
            m2.metric("Gap (units)", c["gap"])
            m3.metric("Hold (recommendation)", f"₹{c['hold']:,.0f}")
            m4.metric("Policy", c["decision"] or "—")
            if c.get("reasons"):
                st.caption(" · ".join(json.loads(c["reasons"])))
            b1, b2 = st.columns(2)
            if b1.button("Investigate (WF3)"):
                call("POST", f"/cases/{sel}/investigate")
                st.rerun()
            if b2.button("Verify now (WF5)"):
                st.write(call("POST", f"/cases/{sel}/verify"))
            if c["status"] == "AWAITING_APPROVAL" and c.get("draft"):
                st.markdown("**Draft to supplier** (numbers from system of record, lint-checked)")
                txt = st.text_area("draft", c["draft"], height=180, label_visibility="collapsed")
                a1, a2 = st.columns(2)
                if a1.button("✅ Approve & send", type="primary"):
                    r = call("POST", f"/cases/{sel}/approve", json={"edited_draft": txt if txt != c["draft"] else None})
                    st.write(r)
                    if r.get("ok") and os.getenv("N8N_SEND_WEBHOOK"):
                        requests.post(os.environ["N8N_SEND_WEBHOOK"], json=r | {"case_id": sel}, timeout=30)
                if a2.button("❌ Reject"):
                    call("POST", f"/cases/{sel}/reject")
                    st.rerun()
            st.markdown("**Timeline**")
            for e in c["timeline"]:
                st.markdown(f"`{e['at'][11:19]}` **{e['type']}** — {e['detail']}")
            st.markdown("**Evidence** (provenance shown; email = claim, not evidence)")
            st.dataframe([{"kind": e["kind"], "source": e["provenance"], "method": e["method"],
                           "claim": bool(e["is_claim"]), "conf": e["confidence"], "data": e["data"]}
                          for e in c["evidence"]], use_container_width=True)

with tab_paths:
    st.caption("Which path the router used for each capability (NATIVE / FALLBACK / MANUAL).")
    st.dataframe(call("GET", "/admin/paths"), use_container_width=True)

with tab_metrics:
    dashboard_dir = Path(__file__).resolve().parent
    repo_root = dashboard_dir.parent

    try:
        sys.path.insert(0, str(dashboard_dir))
        from eval_view import render as render_evaluation

        render_evaluation(repo_root)

    except Exception as e:
        st.warning(
            f"New evaluation view unavailable ({e}); "
            "showing the previous evaluation view."
        )

        p = repo_root / "eval" / "results.json"

        if p.exists():
            st.dataframe(
                json.loads(
                    p.read_text(encoding="utf-8")
                ),
                use_container_width=True,
            )
            st.caption(
                "Synthetic dataset. Not production performance. "
                "No rupee savings claimed."
            )
        else:
            st.info(
                "Run: python -m eval.run_eval --arms R0,R1,S,L"
            )
