"""Notification helper — pushes messages to ntfy or Pushover.

Usage:
  from interfaces.notifier import notify
  notify("Position #47 hit profit target", level="info")
  notify("Iron condor #12 stop loss triggered", level="alert", topic="positions")

Levels: info, warn, alert, critical
"""

from __future__ import annotations

import os
from typing import Literal


Level = Literal["info", "warn", "alert", "critical"]


def notify(
    message: str,
    *,
    level: Level = "info",
    title: str | None = None,
    topic: str | None = None,
    url: str | None = None,
) -> bool:
    """Send a notification via the configured provider.

    Returns True if successfully sent, False otherwise. Errors are silenced
    so a notification failure never breaks the trading system.
    """
    provider = os.environ.get("NOTIFY_PROVIDER", "ntfy")
    try:
        if provider == "ntfy":
            return _ntfy(message, level, title, topic, url)
        if provider == "pushover":
            return _pushover(message, level, title, url)
    except Exception:
        return False
    return False


def _ntfy(message: str, level: Level, title: str | None, topic: str | None, url: str | None) -> bool:
    import httpx
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
    topic = topic or os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    priority_map = {"info": 3, "warn": 4, "alert": 4, "critical": 5}
    headers = {
        "Title": title or "Oil Trader",
        "Priority": str(priority_map.get(level, 3)),
        "Tags": level,
    }
    if url:
        headers["Click"] = url
    httpx.post(f"{server}/{topic}", data=message.encode(), headers=headers, timeout=5)
    return True


def _pushover(message: str, level: Level, title: str | None, url: str | None) -> bool:
    import httpx
    token = os.environ.get("PUSHOVER_APP_TOKEN")
    user = os.environ.get("PUSHOVER_USER_KEY")
    if not (token and user):
        return False
    priority_map = {"info": 0, "warn": 0, "alert": 1, "critical": 2}
    payload = {
        "token": token,
        "user": user,
        "message": message,
        "title": title or "Oil Trader",
        "priority": priority_map.get(level, 0),
    }
    if url:
        payload["url"] = url
    httpx.post("https://api.pushover.net/1/messages.json", data=payload, timeout=5)
    return True
