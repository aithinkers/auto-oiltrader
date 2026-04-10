# Install -- new machine setup

## Prerequisites

- macOS 14+ or Linux (Debian/Ubuntu/Fedora)
- Python 3.12+
- Interactive Brokers account with TWS or IB Gateway installed
- Anthropic API account (for LLM agents)
- (Optional) Tailscale for remote dashboard access

## 1. Clone the repo

```bash
git clone https://github.com/aithinkers/auto-oiltrader.git
cd auto-oiltrader
```

## 2. Install Python deps

Using `uv` (recommended):
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

Or `pip`:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## 3. Configure secrets

```bash
cp .env.example .env
# Edit .env and fill in:
#   ANTHROPIC_API_KEY
#   IB_HOST, IB_PORT, IB_ACCOUNT
#   NTFY_TOPIC (or PUSHOVER credentials)
```

Get an Anthropic API key at https://console.anthropic.com (separate from Claude Code).

## 4. Review settings

Edit `config/settings.toml` to configure:
- Capital limits (starting_capital, daily_loss_halt)
- IB connection parameters
- Display timezone (default: America/New_York)
- Snapshot interval

## 5. Initialize the database

```bash
mkdir -p data logs
python -m cli.tradectl init-db      # creates SQLite DB + schema
python -m cli.tradectl migrate      # apply any pending migrations
python -m cli.tradectl status       # verify: mode=paper, $20k starting
```

The database is SQLite in WAL mode at `data/trader.db`.

## 6. Set up TWS / Gateway

The IB API does not talk to IB's backend directly -- it connects to a local (or network-reachable) **TWS** or **IB Gateway** process. You need one of these running before starting the daemon.

- Open TWS or IB Gateway, log in
- Edit -> Global Configuration -> API -> Settings:
  - Enable ActiveX and Socket Clients
  - Trusted IPs: add 127.0.0.1 (and any remote daemon hosts)
  - Master API Client ID: blank
  - Read-Only API: **off** (needed for order submission)
- Confirm port matches `.env`:
  - 7496 = TWS live
  - 7497 = TWS paper
  - 4001 = Gateway live
  - 4002 = Gateway paper

For paper trading, use the paper account port (7497 or 4002).

### Running on a headless server (no TWS)

For unattended 24/7 operation, install **IB Gateway** directly on the server. Gateway is a headless-friendly version of TWS -- smaller, no charting UI, designed for servers.

**Option A: IB Gateway + IBC + systemd (bare metal)**

1. Download IB Gateway from https://www.interactivebrokers.com/en/trading/ibgateway-stable.php (choose Linux or Windows)
2. Install [IBC (IB Controller)](https://github.com/IbcAlpha/IBC) -- wraps Gateway to handle auto-login, nightly restarts, and the 11:55 PM ET auto-logout
3. Configure IBC:
   ```ini
   # ~/ibc/config.ini
   IbLoginId=YOUR_USERNAME
   IbPassword=YOUR_PASSWORD
   TradingMode=paper       # or "live"
   IbDir=/opt/ibgateway
   ```
4. Create a systemd unit to run IBC on boot:
   ```ini
   # /etc/systemd/system/ibgateway.service
   [Unit]
   Description=IB Gateway via IBC
   After=network.target

   [Service]
   User=trader
   ExecStart=/home/trader/ibc/scripts/ibcstart.sh 10.30 --gateway --mode=paper
   Restart=always
   RestartSec=30

   [Install]
   WantedBy=multi-user.target
   ```
5. Enable: `sudo systemctl enable --now ibgateway`
6. Point the trader daemon at `127.0.0.1:4002` (paper) or `4001` (live)

**Option B: IB Gateway in Docker**

Community-maintained images handle auto-restart and the nightly logout cycle:
```bash
docker run -d --name ibgateway \
  --restart unless-stopped \
  -p 127.0.0.1:4002:4002 \
  -e TWS_USERID=YOUR_USERNAME \
  -e TWS_PASSWORD=YOUR_PASSWORD \
  -e TRADING_MODE=paper \
  ghcr.io/gnzsnz/ib-gateway:latest
```
Then set `IB_HOST=127.0.0.1` and `IB_PORT=4002` in `.env`.

**Option C: TWS on laptop, daemon on server (dev setup)**

Keep TWS running on your laptop and point the server at it via Tailscale or LAN:
```
IB_HOST=100.64.x.y   # Tailscale IP of laptop
IB_PORT=7497
```
Add the server's IP to TWS "Trusted IPs". Downside: laptop has to stay on and logged in.

### Important constraints

- **Nightly auto-logout**: IB logs out every session at ~11:55 PM ET for maintenance. IBC and the Docker images handle this automatically; raw TWS installations do not.
- **Two-factor auth**: live accounts with 2FA require a daily mobile tap unless you use IB's "IBKR Mobile" second-factor method -- which still needs a tap. Paper accounts have no 2FA.
- **Market data subscriptions**: you need the CL/LO futures options data subscription on your IB account regardless of where Gateway runs.
- **One connection per client ID**: each daemon worker uses a different `client_id` (100, 101, 102 by default in settings.toml). Only one process can use a given client ID at a time.

## 7. Start the system

```bash
# Start the daemon (4 worker threads: stream, trader, position, summarizer)
python -m daemons.main --log-level INFO &

# Start the dashboard
streamlit run interfaces/dashboard/app.py --server.port 8511 &
```

Browse to http://localhost:8511 to see the dashboard.

## 8. Tailscale (optional)

For remote access to the dashboard from your phone:

```bash
# macOS
brew install tailscale
sudo tailscale up

# Linux
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then access `http://<machine-name>.tailnet:8511` from any Tailscale-connected device.

## 9. Set up services (optional)

### macOS (launchd)

```bash
cp services/com.oiltrader.stream.plist ~/Library/LaunchAgents/
# edit paths in the plist to match your install
launchctl load ~/Library/LaunchAgents/com.oiltrader.stream.plist
```

### Linux (systemd)

```bash
mkdir -p ~/.config/systemd/user/
cp services/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable trader-stream trader-positions trader-dashboard
systemctl --user start trader-stream trader-positions trader-dashboard
```

## 10. Verify

```bash
python -m cli.tradectl status       # mode=paper, starting capital correct
python -m cli.tradectl positions    # should be empty initially
# Check dashboard at http://localhost:8511
```

## Common gotchas

- **IB must be running first**: TWS/Gateway must be logged in before starting the daemon. The daemon retries connections but needs IB available.
- **macOS firewall**: accept the Streamlit port prompt the first time.
- **DB location**: `data/trader.db` -- back this up regularly once trading.
- **IB auto-logout**: TWS auto-logs out at 11:55 PM ET nightly. Use IB Gateway for unattended operation.
- **Anthropic API key**: separate from Claude Code. Get one at https://console.anthropic.com.

## Halt button

The most important command:

```bash
python -m cli.tradectl halt
```

This sets `mode=halt` and the trader daemon will refuse any new orders. Use it any time you're unsure.
