"""Costs page — commissions, LLM, infra spend tracker."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from interfaces.dashboard.components.db import DB_PATH, db_exists, query_df, query_one


st.title("💰 Cost Tracking")

if not db_exists():
    st.error(f"DB not found at `{DB_PATH}`")
    st.stop()

st.markdown("### Spend by category — current month")
month_df = query_df(
    """
    SELECT category,
           COUNT(*) AS entries,
           SUM(amount) AS total
    FROM costs
    WHERE ts >= strftime('%Y-%m-01', 'now')
    GROUP BY category
    ORDER BY total DESC
    """
)

if month_df.empty:
    st.caption("No costs logged yet this month.")
else:
    total = float(month_df["total"].sum())
    c1, c2, c3 = st.columns(3)
    llm_total = float(month_df[month_df["category"] == "llm"]["total"].sum() or 0)
    comm_total = float(month_df[month_df["category"] == "commission"]["total"].sum() or 0)
    c1.metric("LLM (month-to-date)", f"${llm_total:,.2f}")
    c2.metric("Commissions (MTD)", f"${comm_total:,.2f}")
    c3.metric("Total (MTD)", f"${total:,.2f}")
    st.dataframe(month_df, use_container_width=True, hide_index=True)

st.markdown("---")
st.markdown("### Spend by category — all time")
all_df = query_df(
    """
    SELECT category, COUNT(*) AS entries, SUM(amount) AS total
    FROM costs GROUP BY category ORDER BY total DESC
    """
)
if not all_df.empty:
    st.dataframe(all_df, use_container_width=True, hide_index=True)

st.markdown("### Recent cost entries")
recent = query_df(
    "SELECT ts, category, detail, amount, context FROM costs ORDER BY ts DESC LIMIT 50"
)
if recent.empty:
    st.caption("No cost entries yet.")
else:
    st.dataframe(recent, use_container_width=True, hide_index=True)

st.markdown("---")
st.markdown("### Net P&L (after costs)")
realized_row = query_one("SELECT COALESCE(SUM(realized_pnl), 0) AS r FROM positions WHERE status='closed'")
costs_row = query_one("SELECT COALESCE(SUM(amount), 0) AS c FROM costs")
gross = float(realized_row["r"] if realized_row else 0)
costs_total = float(costs_row["c"] if costs_row else 0)
net = gross - costs_total

c1, c2, c3 = st.columns(3)
c1.metric("Gross trading P&L", f"${gross:,.2f}")
c2.metric("Total costs", f"${costs_total:,.2f}")
c3.metric("Net P&L", f"${net:,.2f}", delta=f"{net/max(1,abs(gross))*100:+.0f}% of gross")
