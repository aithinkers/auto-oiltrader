"""Positions page — open positions, marks, P&L, exits."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from interfaces.dashboard.components.db import DB_PATH, db_exists, query_df


def _describe_legs(legs_json: str) -> str:
    """Turn legs JSON into a readable string like 'CL May14 SELL 80P / BUY 78P'."""
    try:
        legs = json.loads(legs_json)
    except (json.JSONDecodeError, TypeError):
        return legs_json
    parts = []
    symbol = ""
    expiry_label = ""
    for leg in legs:
        symbol = leg.get("symbol", "")
        raw_exp = leg.get("expiry", "")
        if len(raw_exp) == 8:
            from datetime import datetime
            try:
                dt = datetime.strptime(raw_exp, "%Y%m%d")
                expiry_label = dt.strftime("%b%d")
            except ValueError:
                expiry_label = raw_exp
        action = leg.get("action", "")
        strike = leg.get("strike", "")
        right = leg.get("right", "")
        if isinstance(strike, float) and strike == int(strike):
            strike = int(strike)
        parts.append(f"{action} {strike}{right}")
    return f"{symbol} {expiry_label} {' / '.join(parts)}"


st.title("📈 Positions")

if not db_exists():
    st.error(f"DB not found at `{DB_PATH}`")
    st.stop()

st.markdown("### Open positions")
open_df = query_df(
    """
    SELECT id, strategy_id, structure, legs, qty, open_debit, ts_opened, mode
    FROM positions
    WHERE status = 'open'
    ORDER BY ts_opened DESC
    """
)
if open_df.empty:
    st.caption("No open positions.")
else:
    open_df.insert(3, "description", open_df["legs"].apply(_describe_legs))
    open_df = open_df.drop(columns=["legs"])
    st.dataframe(open_df, use_container_width=True, hide_index=True)

    st.markdown("### Latest marks")
    marks_df = query_df(
        """
        WITH latest AS (
          SELECT position_id, ts, mark, unrealized_pnl, delta, gamma, vega, theta,
                 ROW_NUMBER() OVER (PARTITION BY position_id ORDER BY ts DESC) AS rn
          FROM position_marks
        )
        SELECT p.id, p.structure, p.qty, p.open_debit,
               l.mark, l.unrealized_pnl, l.delta, l.gamma, l.vega, l.theta, l.ts
        FROM positions p
        LEFT JOIN latest l ON l.position_id = p.id AND l.rn = 1
        WHERE p.status = 'open'
        ORDER BY p.ts_opened DESC
        """
    )
    if not marks_df.empty:
        st.dataframe(marks_df, use_container_width=True, hide_index=True)

st.markdown("### Closing positions")
closing_df = query_df(
    """
    SELECT id, strategy_id, structure, legs, qty, open_debit, ts_opened
    FROM positions
    WHERE status = 'closing'
    """
)
if closing_df.empty:
    st.caption("No positions in closing state.")
else:
    closing_df.insert(3, "description", closing_df["legs"].apply(_describe_legs))
    closing_df = closing_df.drop(columns=["legs"])
    st.dataframe(closing_df, use_container_width=True, hide_index=True)

st.markdown("### Recently closed")
closed_df = query_df(
    """
    SELECT id, strategy_id, structure, legs, qty, open_debit, close_credit,
           realized_pnl, exit_reason, ts_closed
    FROM positions
    WHERE status = 'closed'
    ORDER BY ts_closed DESC
    LIMIT 50
    """
)
if closed_df.empty:
    st.caption("No closed positions yet.")
else:
    closed_df.insert(3, "description", closed_df["legs"].apply(_describe_legs))
    closed_df = closed_df.drop(columns=["legs"])
    st.dataframe(closed_df, use_container_width=True, hide_index=True)

    total = float(closed_df["realized_pnl"].sum())
    wins = int((closed_df["realized_pnl"] > 0).sum())
    losses = int((closed_df["realized_pnl"] < 0).sum())
    win_rate = wins / max(1, wins + losses)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total realized P&L", f"${total:,.0f}")
    c2.metric("Wins", wins)
    c3.metric("Losses", losses)
    c4.metric("Win rate", f"{win_rate:.0%}")
