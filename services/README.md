# Services

Templates for running the daemons under systemd (Linux) or launchd (macOS).
Symlink the appropriate ones into the right location at install time.

## Linux (systemd)

Copy the `.service` files to `~/.config/systemd/user/` and:

```bash
systemctl --user daemon-reload
systemctl --user enable trader-stream.service
systemctl --user start trader-stream.service
```

## macOS (launchd)

Copy the `.plist` files to `~/Library/LaunchAgents/` and:

```bash
launchctl load ~/Library/LaunchAgents/com.oiltrader.stream.plist
launchctl start com.oiltrader.stream
```

## Files

- `trader-stream.service` — stream daemon (always on during RTH + extended hours)
- `trader-news.service` — news collector
- `trader-positions.service` — position manager
- `trader-daemon.service` — order/state machine
- `trader-dashboard.service` — Streamlit dashboard
- `trader-api.service` — FastAPI server

All services should be set to restart on failure with a 30-second delay.
