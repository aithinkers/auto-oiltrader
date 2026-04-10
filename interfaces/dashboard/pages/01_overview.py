"""Overview page — capital, daily P&L, equity curve, recent commentary."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from interfaces.dashboard.components.db import DB_PATH, db_exists, query_df, query_one


st.title("📊 Overview")

if not db_exists():
    st.error(f"DB not found at `{DB_PATH}`")
    st.stop()

cash = query_one("SELECT * FROM cash ORDER BY ts DESC LIMIT 1")
if not cash:
    st.warning("No cash row in DB")
    st.stop()
assert cash is not None  # for type narrowing

mode_color = {"paper": "blue", "draft": "orange", "live": "green", "halt": "red"}.get(cash["mode"], "gray")
st.markdown(f"### Mode: :{mode_color}[**{cash['mode'].upper()}**]")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Starting capital", f"${float(cash['starting_capital']):,.2f}")
m2.metric("Current balance", f"${float(cash['current_balance']):,.2f}",
          delta=f"{float(cash['current_balance']) - float(cash['starting_capital']):+,.2f}")
m3.metric("High watermark", f"${float(cash['high_watermark']):,.2f}")
m4.metric("Daily P&L", f"${float(cash['daily_pnl']):,.2f}",
          delta=f"halt @ -${float(cash['daily_loss_halt']):,.0f}")

st.markdown("### Equity curve")
equity = query_df("SELECT ts, current_balance FROM cash ORDER BY ts")
if len(equity) > 1:
    equity["ts"] = equity["ts"].astype(str)
    st.line_chart(equity.set_index("ts")["current_balance"])
else:
    st.caption("Only one cash row so far — equity curve will appear after trades execute.")

st.markdown("### Recent commentary")
commentary = query_df(
    "SELECT ts, level, topic, message FROM commentary ORDER BY ts DESC LIMIT 30"
)
if commentary.empty:
    st.caption("No commentary yet.")
else:
    for _, row in commentary.iterrows():
        icon = {"info": "ℹ️", "warn": "⚠️", "alert": "🚨", "critical": "🔴"}.get(row["level"], "•")
        topic = f"[{row['topic']}] " if row["topic"] else ""
        st.text(f"{icon} {row['ts']}  {topic}{row['message']}")

st.markdown("---")
st.markdown(
    """
    ### Quick actions
    - Halt trading: `python -m cli.tradectl halt`
    - Resume: `python -m cli.tradectl unhalt`
    - Submit observation: `python -m cli.tradectl observe "..."`
    - Cost summary: `python -m cli.tradectl costs --month`
    """
)
