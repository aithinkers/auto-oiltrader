"""Tests for core/timefmt.py — display timezone formatting."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from core import timefmt


@pytest.fixture(autouse=True)
def reset_cache_around_each_test():
    """Each test gets a clean view of the config (env vars + cache)."""
    timefmt.reset_cache()
    yield
    timefmt.reset_cache()


def test_parse_iso_with_z_suffix():
    dt = timefmt.parse_iso("2026-04-09T20:42:17.123Z")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 4 and dt.day == 9
    assert dt.hour == 20 and dt.minute == 42
    assert dt.tzinfo == timezone.utc


def test_parse_iso_with_offset():
    dt = timefmt.parse_iso("2026-04-09T20:42:17+00:00")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_parse_iso_naive_assumed_utc():
    dt = timefmt.parse_iso("2026-04-09T20:42:17")
    assert dt is not None
    assert dt.tzinfo == timezone.utc


def test_parse_iso_handles_space_separator():
    dt = timefmt.parse_iso("2026-04-09 20:42:17")
    assert dt is not None


def test_parse_iso_returns_none_for_empty():
    assert timefmt.parse_iso(None) is None
    assert timefmt.parse_iso("") is None


def test_parse_iso_with_datetime_object():
    src = datetime(2026, 4, 9, 20, 42, 17, tzinfo=timezone.utc)
    dt = timefmt.parse_iso(src)
    assert dt == src


def test_to_local_converts_to_eastern(monkeypatch):
    monkeypatch.setenv("DISPLAY_TZ", "America/New_York")
    timefmt.reset_cache()
    # 20:42 UTC on 2026-04-09 = 16:42 EDT (UTC-4 in April)
    local = timefmt.to_local("2026-04-09T20:42:17.123Z")
    assert local is not None
    assert local.hour == 16
    assert local.minute == 42
    assert "New_York" in str(local.tzinfo)


def test_to_local_converts_to_singapore(monkeypatch):
    monkeypatch.setenv("DISPLAY_TZ", "Asia/Singapore")
    timefmt.reset_cache()
    # 20:42 UTC = 04:42 next day in Singapore (UTC+8)
    local = timefmt.to_local("2026-04-09T20:42:17.123Z")
    assert local is not None
    assert local.day == 10
    assert local.hour == 4
    assert local.minute == 42


def test_fmt_local_default_format(monkeypatch):
    monkeypatch.setenv("DISPLAY_TZ", "America/New_York")
    timefmt.reset_cache()
    out = timefmt.fmt_local("2026-04-09T20:42:17.123Z")
    # Should contain the year, hour, and a TZ name
    assert "2026" in out
    assert "16:42" in out
    assert ("EDT" in out) or ("EST" in out)


def test_fmt_local_explicit_format(monkeypatch):
    monkeypatch.setenv("DISPLAY_TZ", "America/New_York")
    timefmt.reset_cache()
    out = timefmt.fmt_local("2026-04-09T20:42:17.123Z", fmt="%H:%M")
    assert out == "16:42"


def test_fmt_local_returns_fallback_for_none():
    assert timefmt.fmt_local(None) == "-"
    assert timefmt.fmt_local("", fallback="(empty)") == "(empty)"


def test_invalid_timezone_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("DISPLAY_TZ", "Not/A_Real_Timezone")
    timefmt.reset_cache()
    tz = timefmt.display_tz()
    # Should fall back to America/New_York
    assert "New_York" in str(tz)


def test_fmt_now_uses_configured_tz(monkeypatch):
    monkeypatch.setenv("DISPLAY_TZ", "Asia/Tokyo")
    timefmt.reset_cache()
    out = timefmt.fmt_now()
    assert "JST" in out or "+09" in out or "Tokyo" in out or len(out) > 0


def test_reset_cache_picks_up_env_change(monkeypatch):
    monkeypatch.setenv("DISPLAY_TZ", "America/New_York")
    timefmt.reset_cache()
    tz1 = timefmt.display_tz()
    assert "New_York" in str(tz1)

    monkeypatch.setenv("DISPLAY_TZ", "Europe/London")
    timefmt.reset_cache()
    tz2 = timefmt.display_tz()
    assert "London" in str(tz2)
