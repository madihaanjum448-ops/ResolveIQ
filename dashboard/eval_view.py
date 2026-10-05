"""Evaluation tab: golden-set results first, template-generated set second."""

import json
from pathlib import Path

import streamlit as st


SHOW = [
    "arm",
    "n",
    "class_acc",
    "action_acc",
    "email_dep_acc",
    "non_english_acc",
    "premature_dispute",
    "unsafe_actions",
    "injection_flagged",
    "invented_eta",
    "language_id_acc",
    "llm_errors",
]


def render(root: Path):
    ev = root / "eval"

    gp = ev / "golden_results.json"
    rp = ev / "golden_report.md"
    tp = ev / "results.json"

    st.subheader("Golden set: hand-labelled, test only")

    if not gp.exists():
        st.info(
            "Run: python -m eval.golden_eval "
            "--arms R0,R1,R1P,S,L --sleep 7"
        )
    else:
        data = json.loads(
            gp.read_text(encoding="utf-8")
        )

        rows = [
            {k: r.get(k) for k in SHOW}
            for r in data["summary"]
        ]

        st.dataframe(
            rows,
            use_container_width=True,
        )

        chart_data = {
            "class accuracy": {
                r["arm"]: r["class_acc"]
                for r in data["summary"]
                if r["class_acc"] is not None
            }
        }

        st.bar_chart(chart_data)

        st.caption(
            f"{data['meta']['n']} cases, labels written by hand "
            "before any run. ORACLE (true extraction through the "
            "same engine) is a plumbing check, not a result. "
            "R1P is a smarter rules baseline tuned on these cases, "
            "so it is optimistic."
        )

        arms = [
            r["arm"]
            for r in data["summary"]
        ]

        default_arms = [
            a
            for a in arms
            if a not in ("R0", "ORACLE")
        ]

        pick = st.multiselect(
            "Show per-case results for",
            arms,
            default=default_arms,
        )

        only_fail = st.checkbox(
            "Only cases where a selected arm failed",
            value=True,
        )

        table = []

        for c in data["cases"]:
            cells = {}

            for a in pick:
                case_data = c["arms"][a]

                if case_data["ok"]:
                    cells[a] = "✓"
                else:
                    cells[a] = (
                        f"✗ {case_data['cls']}"
                    )

            if (
                only_fail
                and pick
                and all(
                    v == "✓"
                    for v in cells.values()
                )
            ):
                continue

            table.append(
                {
                    "id": c["id"],
                    "scenario": c["title"],
                    "lang": c["language"],
                    "expected": c["label"],
                    **cells,
                }
            )

        st.dataframe(
            table,
            use_container_width=True,
        )

        if rp.exists():
            with st.expander(
                "Full report with failing emails and extracted fields"
            ):
                st.markdown(
                    rp.read_text(
                        encoding="utf-8"
                    )
                )

    st.divider()

    st.subheader(
        "Template-generated set (dev/test split)"
    )

    if tp.exists():
        template_data = json.loads(
            tp.read_text(
                encoding="utf-8"
            )
        )

        st.dataframe(
            template_data,
            use_container_width=True,
        )

        st.caption(
            "Generated from templates, so the emails are "
            "easier than real ones. Use for tuning, not as "
            "the headline."
        )
    else:
        st.info(
            "Run: python -m eval.run_eval --arms R0,R1,S,L"
        )

    st.caption(
        "Synthetic data. Not production performance. "
        "No rupee savings claimed."
    )
