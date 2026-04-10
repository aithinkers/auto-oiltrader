"""LLM-powered decision makers.

Each agent reads the DB and current market state, calls Claude with a focused
prompt (loaded from skills/), and writes a decision row plus an action
(recommendation, order, commentary line, etc.) back.

All agents use core.costs to log token usage. The trader_daemon halts agents
if monthly LLM budget is exceeded.
"""
