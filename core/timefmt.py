"""Display-time formatting helpers.

The system stores all timestamps in UTC (so they sort lexicographically and
avoid DST headaches). All HUMAN-FACING output should go through `fmt_local`
or `to_local`, which read the configured display timezone from settings.toml
[display] section.

Usage:

    from core.timefmt import fmt_local, to_local

    # In a dashboard or CLI:
    fmt_local("2026-04-09T20:42:17.123Z")        # → "2026-04-09 16:42 EDT"
    fmt_local("2026-04-09T20:42:17.123Z", "%H:%M %Z")  # → "16:42 EDT"
    to_local(datetime_obj)                         # → datetime in local TZ

The configured timezone is read once and cached. Default: America/New_York.
Override via DISPLAY_TZ env var (useful for tests) or by editing settings.toml.
"""

from __future__ import annotations

import os
import threading
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo


_LOCK = threading.Lock()
_CACHE: dict[str, object] = {}


def _settings_path() -> Path:
    """Locate config/settings.toml relative to the project root."""
    p = Path(__file__).resolve().parent.parent / "config" / "settings.toml"
    return p


def _load_display_config() -> dict:
    """Read [display] from settings.toml. Cached after first call."""
    if "config" in _CACHE:
        return _CACHE["config"]  # type: ignore
    path = _settings_path()
    cfg = {
        "timezone": "America/New_York",
        "timestamp_format": "%Y-%m-%d %H:%M %Z",
        "short_format": "%H:%M %Z",
    }
    if path.exists():
        try:
            with open(path, "rb") as f:
                full = tomllib.load(f)
            cfg.update(full.get("display", {}))
        except Exception:
            pass
    # Env var override (useful for tests + ad-hoc CLI runs)
    env_tz = os.environ.get("DISPLAY_TZ")
    if env_tz:
        cfg["timezone"] = env_tz
    _CACHE["config"] = cfg
    return cfg


def reset_cache() -> None:
    """Drop the cached config. Call after editing settings.toml at runtime."""
    with _LOCK:
        _CACHE.clear()


def display_tz() -> ZoneInfo:
    """Return the configured display timezone as a ZoneInfo."""
    cfg = _load_display_config()
    name = cfg.get("timezone", "America/New_York")
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("America/New_York")


def parse_iso(value: str | datetime | None) -> Optional[datetime]:
    """Parse a stored timestamp string into an aware datetime in UTC.

    Accepts:
      - datetime object (returned as-is, but made tz-aware as UTC if naive)
      - ISO 8601 string with 'Z' suffix (the canonical form we store)
      - ISO 8601 string with explicit offset
      - Naive ISO string (assumed UTC)
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    s = str(value).strip()
    if not s:
        return None
    # Python's fromisoformat handles 'Z' as of 3.11, but be defensive
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Maybe space-separated like "2026-04-09 20:42:17"
        try:
            dt = datetime.fromisoformat(s.replace(" ", "T"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_local(value: str | datetime | None) -> Optional[datetime]:
    """Parse a stored timestamp and convert to the configured display timezone."""
    utc = parse_iso(value)
    if utc is None:
        return None
    return utc.astimezone(display_tz())


def fmt_local(
    value: str | datetime | None,
    fmt: Optional[str] = None,
    fallback: str = "-",
) -> str:
    """Format a stored timestamp in the configured display timezone."""
    local = to_local(value)
    if local is None:
        return fallback
    cfg = _load_display_config()
    fmt_str = fmt or cfg.get("timestamp_format", "%Y-%m-%d %H:%M %Z")
    return local.strftime(fmt_str)


def fmt_local_short(value: str | datetime | None, fallback: str = "-") -> str:
    """Format a stored timestamp using the configured short format."""
    cfg = _load_display_config()
    return fmt_local(value, fmt=cfg.get("short_format", "%H:%M %Z"), fallback=fallback)


def fmt_now() -> str:
    """Return the current time in the configured display TZ + format."""
    cfg = _load_display_config()
    fmt_str = cfg.get("timestamp_format", "%Y-%m-%d %H:%M %Z")
    return datetime.now(display_tz()).strftime(fmt_str)
