"""Recommendations page — pending, approved, rejected with reason."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st

from core.db import approve_draft_recommendation, reject_draft_recommendation
from interfaces.dashboard.components.db import DB_PATH, db_exists, query_df


st.title("💡 Recommendations")

if not db_exists():
    st.error(f"DB not found at `{DB_PATH}`")
    st.stop()

status_filter = st.sidebar.multiselect(
    "Status filter",
    ["pending", "approved", "executed", "rejected", "expired"],
    default=["pending", "approved", "executed"],
)
source_filter = st.sidebar.text_input("Source contains", value="")

if not status_filter:
    st.caption("Pick at least one status in the sidebar.")
    st.stop()

placeholders = ", ".join(["?"] * len(status_filter))
params: list = list(status_filter)
sql = f"""
    SELECT r.id, r.ts, r.source, r.strategy_id, r.structure, r.size_units, r.target_debit,
           r.max_loss, r.max_profit, r.expected_value, r.expiry_date, r.confidence,
           r.status, r.approved_by, r.approved_at, r.rejection_reason, r.thesis,
           (
               SELECT o.id
               FROM orders o
               WHERE o.recommendation_id = r.id
               ORDER BY o.id DESC
               LIMIT 1
           ) AS order_id,
           (
               SELECT o.status
               FROM orders o
               WHERE o.recommendation_id = r.id
               ORDER BY o.id DESC
               LIMIT 1
           ) AS order_status
    FROM recommendations r
    WHERE r.status IN ({placeholders})
"""
if source_filter:
    sql += " AND r.source LIKE ?"
    params.append(f"%{source_filter}%")
sql += " ORDER BY r.ts DESC LIMIT 200"

df = query_df(sql, params)

if df.empty:
    st.caption("No recommendations match the filter.")
    st.stop()

st.caption(f"{len(df)} recommendations")

for _, row in df.iterrows():
    with st.expander(
        f"#{row['id']}  [{row['status']}]  {row['structure']}  "
        f"(source={row['source']}, debit={float(row['target_debit'] or 0):.2f}, "
        f"conf={float(row['confidence'] or 0):.2f})"
    ):
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Max profit", f"${float(row['max_profit'] or 0):,.0f}")
        c2.metric("Max loss", f"${float(row['max_loss'] or 0):,.0f}")
        c3.metric("Size", int(row["size_units"]))
        c4.metric("Confidence", f"{float(row['confidence'] or 0):.0%}")
        st.write(row["thesis"])
        order_id = row.get("order_id")
        order_status = row.get("order_status")
        if order_id == order_id:
            st.caption(f"Latest order: #{int(order_id)} [{order_status or 'unknown'}]")
        if row["status"] == "rejected" and row.get("rejection_reason"):
            st.error(f"Rejected: {row['rejection_reason']}")
        elif row["status"] == "approved":
            approved_by = row["approved_by"] or "unknown"
            approved_at = row["approved_at"] or "-"
            st.success(f"Approved by {approved_by} at {approved_at}")
        elif row["status"] == "executed":
            st.info(f"Executed by {row['approved_by']}")

        if row["status"] == "pending" and order_status == "draft":
            approve_col, reject_col = st.columns(2)
            actor = st.text_input(
                "Actor",
                value="dashboard",
                key=f"actor_{int(row['id'])}",
            )
            reason = st.text_input(
                "Reject reason",
                value="manual rejection",
                key=f"reason_{int(row['id'])}",
            )
            if approve_col.button("Approve", key=f"approve_{int(row['id'])}"):
                ok, message = approve_draft_recommendation(DB_PATH, int(row["id"]), actor.strip() or "dashboard")
                if ok:
                    st.success(message)
                    st.rerun()
                else:
                    st.error(message)
            if reject_col.button("Reject", key=f"reject_{int(row['id'])}"):
                ok, message = reject_draft_recommendation(
                    DB_PATH,
                    int(row["id"]),
                    actor.strip() or "dashboard",
                    reason.strip() or "manual rejection",
                )
                if ok:
                    st.warning(message)
                    st.rerun()
                else:
                    st.error(message)
