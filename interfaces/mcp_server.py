"""MCP server for Claude Desktop integration.

Exposes a small set of tools so you can chat with Claude Desktop and have it
query/control the trading system. Configure in Claude Desktop settings.json:

  {
    "mcpServers": {
      "oiltrader": {
        "command": "python",
        "args": ["-m", "interfaces.mcp_server"],
        "cwd": "/path/to/autonomous-oiltrader",
        "env": {"TRADER_DB_PATH": "/path/to/data/trader.duckdb"}
      }
    }
  }

Tools exposed (Phase 2+):
  - get_status()
  - get_positions()
  - get_recommendations(status='pending')
  - get_recent_decisions(limit=10)
  - get_recent_commentary(limit=20)
  - approve_recommendation(id)
  - reject_recommendation(id, reason)
  - submit_observation(text, weight, expires_hours)
  - halt_trading()
  - get_findings_latest()

The MCP server is the secure interface between Claude Desktop and the trading
system. Claude does NOT get raw shell or DB access — it can only call these tools.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError("Phase 2: implement after FastAPI endpoints are stable")


if __name__ == "__main__":
    main()
