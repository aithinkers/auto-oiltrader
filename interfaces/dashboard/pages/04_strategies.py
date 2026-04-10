"""Strategies page — library, tier, performance summary."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from interfaces.dashboard.components.db import DB_PATH, db_exists, query_df


st.title("🧠 Strategy Library")

if not db_exists():
    st.error(f"DB not found at `{DB_PATH}`")
    st.stop()

st.markdown("### Active strategies (from `config/strategies.yaml`)")
yaml_path = Path("config/strategies.yaml")
if yaml_path.exists():
    import yaml
    cfg = yaml.safe_load(yaml_path.read_text()) or {}
    rows = cfg.get("strategies", [])
    if rows:
        display = [
            {
                "id": r["id"],
                "name": r["name"],
                "tier": r["tier"],
                "enabled": r.get("enabled", False),
                "max_concurrent": r.get("params", {}).get("max_concurrent", 1),
            }
            for r in rows
        ]
        st.dataframe(display, use_container_width=True, hide_index=True)
    else:
        st.caption("No strategies in yaml.")
else:
    st.warning("config/strategies.yaml not found")

st.markdown("---")
st.markdown("### Per-strategy P&L (from closed positions)")

perf = query_df(
    """
    SELECT strategy_id,
           COUNT(*) AS trades,
           SUM(realized_pnl) AS total_pnl,
           AVG(realized_pnl) AS avg_pnl,
           SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
           SUM(CASE WHEN realized_pnl < 0 THEN 1 ELSE 0 END) AS losses
    FROM positions
    WHERE status = 'closed' AND strategy_id IS NOT NULL
    GROUP BY strategy_id
    ORDER BY total_pnl DESC
    """
)

if perf.empty:
    st.caption("No closed trades yet — strategy P&L will appear after positions close.")
else:
    perf["win_rate"] = perf["wins"] / (perf["wins"] + perf["losses"]).clip(lower=1)
    st.dataframe(perf, use_container_width=True, hide_index=True)

st.markdown("---")
st.markdown("### Strategy events (promotions, parameter changes)")
events = query_df(
    """
    SELECT ts, strategy_id, event, from_tier, to_tier, reason
    FROM strategy_events
    ORDER BY ts DESC LIMIT 50
    """
)
if events.empty:
    st.caption("No strategy events yet.")
else:
    st.dataframe(events, use_container_width=True, hide_index=True)
