"""News & Observations page."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from interfaces.dashboard.components.db import DB_PATH, db_exists, query_df


st.title("📰 News & Observations")

if not db_exists():
    st.error(f"DB not found at `{DB_PATH}`")
    st.stop()

st.markdown("### Recent news (last 24h)")
news = query_df(
    """
    SELECT ts, source, headline, impact, sentiment, url
    FROM news
    WHERE ts >= datetime('now', '-1 day')
    ORDER BY ts DESC
    LIMIT 100
    """
)
if news.empty:
    st.caption("No news in the last 24h.")
else:
    for _, row in news.iterrows():
        impact_color = {
            "low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"
        }.get(row.get("impact"), "⚪")
        line = f"{impact_color} `{row['ts']}` **[{row['source']}]** {row['headline']}"
        if row.get("url"):
            line += f"  ([link]({row['url']}))"
        st.markdown(line)

st.markdown("---")
st.markdown("### Active user observations")
obs = query_df(
    """
    SELECT id, ts, text, category, weight, expires_at
    FROM user_observations
    WHERE expires_at > datetime('now')
    ORDER BY ts DESC
    """
)
if obs.empty:
    st.caption("No active observations.")
else:
    st.dataframe(obs, use_container_width=True, hide_index=True)

st.markdown("---")
st.markdown("### Add observation")
st.caption("To submit a new observation, use `python -m cli.tradectl observe \"...\"`.")

st.markdown("---")
st.markdown("### Active patterns")
patterns = query_df(
    """
    SELECT name, description, category, weight, active, times_referenced
    FROM patterns
    WHERE active = 1
    ORDER BY weight DESC
    """
)
if patterns.empty:
    st.caption("No active patterns.")
else:
    for _, row in patterns.iterrows():
        with st.expander(f"**{row['name']}**  (weight={float(row['weight']):.2f}, category={row['category']})"):
            st.write(row["description"])
            st.caption(f"Referenced {row['times_referenced']} times")
