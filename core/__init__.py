"""Core business logic — pure modules with no harness or daemon dependencies.

Every importable here is safe to call from CLI, dashboards, agents, or daemons.
No module in `core/` may have side effects on import.
"""
