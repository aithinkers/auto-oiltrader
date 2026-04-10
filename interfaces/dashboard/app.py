"""Streamlit dashboard — main entry.

Run:
  streamlit run interfaces/dashboard/app.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sure project root is on sys.path so we can import the components helper
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from interfaces.dashboard.components.db import db_exists, query_one


st.set_page_config(
    page_title="Autonomous Oil Trader",
    page_icon="🛢️",
    layout="wide",
)

st.title("🛢️ Autonomous Oil Trader")

if not db_exists():
    from interfaces.dashboard.components.db import DB_PATH
    st.error(f"Database not found at `{DB_PATH}`.")
    st.info("Run `python -m cli.tradectl init-db` to initialize.")
    st.stop()

cash = query_one(
    "SELECT mode, current_balance, starting_capital, daily_pnl, daily_loss_halt "
    "FROM cash ORDER BY ts DESC LIMIT 1"
)
if cash:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Mode", cash["mode"].upper())
    c2.metric("Balance", f"${float(cash['current_balance']):,.2f}")
    c3.metric("Starting", f"${float(cash['starting_capital']):,.2f}")
    c4.metric("Daily P&L", f"${float(cash['daily_pnl']):,.2f}",
              delta=f"halt at -${float(cash['daily_loss_halt']):,.0f}")
    n_open = query_one("SELECT COUNT(*) AS n FROM positions WHERE status='open'")
    c5.metric("Open positions", int(n_open["n"]) if n_open else 0)

st.markdown("---")
st.markdown(
    """
    Use the **sidebar** to navigate to:
    - **Overview** — system mode, capital, halt button
    - **Positions** — open positions, marks, greeks
    - **Recommendations** — strategy signals, draft orders
    - **Strategies** — strategy library, tier management
    - **News & Observations** — news feed, observation entry
    - **Findings** — weekly performance reports
    - **Costs** — commission + LLM spend tracker

    Run `python -m cli.tradectl --help` to see CLI commands.
    """
)
