"""Hourly summaries — read-only view of past digests."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from interfaces.dashboard.components.db import DB_PATH, db_exists, query_df


st.title("📋 Hourly Summaries")

if not db_exists():
    st.error(f"DB not found at `{DB_PATH}`")
    st.stop()

n_show = st.sidebar.slider("Number of summaries to show", 1, 50, 5)

df = query_df(
    """
    SELECT id, ts, period_start, period_end, headline, body_md, pushed
    FROM summaries
    ORDER BY ts DESC
    LIMIT ?
    """,
    [n_show],
)

if df.empty:
    st.caption(
        "No summaries yet. Either the daemon hasn't reached its first tick, "
        "or the summarizer is disabled in settings.toml. "
        "Run `python -m cli.tradectl summary --now` to build one immediately."
    )
    st.stop()

st.caption(f"{len(df)} summaries")

for _, row in df.iterrows():
    title = f"#{row['id']}  {row['ts']}  —  {row['headline']}"
    if row.get("pushed"):
        title += "  📲"
    with st.expander(title, expanded=(row.name == 0)):  # type: ignore
        st.markdown(row["body_md"])
