"""Findings page — weekly evaluator reports."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from interfaces.dashboard.components.db import DB_PATH, db_exists, query_df


st.title("📋 Weekly Findings")

if not db_exists():
    st.error(f"DB not found at `{DB_PATH}`")
    st.stop()

findings = query_df(
    """
    SELECT id, ts, period_start, period_end, report_md, approved_by, approved_at
    FROM findings
    ORDER BY ts DESC
    LIMIT 20
    """
)

if findings.empty:
    st.caption(
        "No findings reports yet. The evaluator agent (Phase 3) will write a "
        "report every Friday after market close."
    )
else:
    for _, row in findings.iterrows():
        title = f"Week ending {row['period_end']}"
        if row.get("approved_by"):
            title += f" ✓ approved by {row['approved_by']}"
        with st.expander(title):
            st.markdown(row["report_md"])
