# Polymarket World Cup Underdog Large Trade Alert System

An explainable, read-only alerting system for large underdog trades in Polymarket World Cup prediction markets.

It is read-only:

- No trading.
- No wallet login.
- No private key.
- No order signing.
- Data comes from public Polymarket APIs.

Chinese documentation is available in [README.md](README.md). The detailed alert algorithm is documented in [docs/ALGORITHM_EN.md](docs/ALGORITHM_EN.md).

## 1. Architecture

```text
Polymarket public APIs
  Gamma REST           -> market discovery
  CLOB REST            -> prices, order books, fallback trades
  CLOB market WS       -> live market updates

Server
  MonitoringEngine     -> discovers markets, subscribes to tokens, polls fallbacks
  Detector             -> computes underdog_score and alert level
  Notifier             -> stores alerts in SQLite and pushes them to clients

Windows Client
  Tkinter UI           -> connection status, alert list, settings
  Native notifications -> Windows Toast and sound alerts
```

## 2. Directory Layout

```text
server/
  main.py                  Orchestration entry point
  config.py                Settings loading and validation
  config.yaml.example      Server config template
  polymarket_client.py     Public REST and WS API wrapper
  detector.py              Underdog scoring and alert logic
  notifier.py              Authenticated WebSocket server
  db.py                    SQLite alert storage

client/
  windows_client.py        Windows desktop client
  config.json.example      Client config template

deploy/
  polymarket-alert.service systemd service example
  build_exe.bat            Windows PyInstaller build script

docs/
  ALGORITHM.md             Chinese algorithm guide
  ALGORITHM_EN.md          English algorithm guide

tests/
  test_detector.py         Unit tests for scoring logic
  test_pipeline.py         End-to-end authenticated push test
  test_connectivity.py     Public Polymarket API connectivity test
```

## 3. Server Setup

Requirements:

- Ubuntu 22.04 or 24.04 LTS
- Python 3.11 or newer

Install:

```bash
cd /opt/polymarket-wc-alert/server

python3.12 -m venv ../venv
source ../venv/bin/activate
pip install -U pip
pip install -r requirements.txt

cp config.yaml.example config.yaml
nano config.yaml
```

At minimum, change:

```yaml
server:
  auth_token: "replace-with-a-strong-random-token"
```

Generate a strong token:

```bash
openssl rand -hex 24
```

Run in the foreground:

```bash
python main.py --config ./config.yaml
```

## 4. systemd Deployment

Copy and edit the service file:

```bash
sudo cp /opt/polymarket-wc-alert/deploy/polymarket-alert.service /etc/systemd/system/
sudo nano /etc/systemd/system/polymarket-alert.service
```

Then enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-alert
sudo systemctl status polymarket-alert
```

Logs:

```bash
journalctl -u polymarket-alert -f
tail -f /opt/polymarket-wc-alert/server/logs/server.log
```

## 5. Windows Client

Install and run from source:

```bat
cd client
copy config.json.example config.json
notepad config.json
pip install -r requirements.txt
python windows_client.py
```

Example `config.json`:

```json
{
  "server_url": "ws://127.0.0.1:8765",
  "token": "same-token-as-server",
  "sound_enabled": true,
  "popup_enabled": true,
  "minimize_to_tray": true,
  "insecure_skip_verify": false
}
```

For a public deployment, use TLS and set `server_url` to a `wss://` endpoint.

Build an exe:

```bat
deploy\build_exe.bat
```

The output is `dist\PolymarketAlert.exe`. Put `config.json` in the same directory as the exe.

## 6. TLS Recommendation

For public deployments, do not expose plain `ws://` directly to the internet. Use one of these patterns:

- Caddy or Nginx reverse proxy with Let's Encrypt, public `wss://` on port 443.
- Direct TLS in the Python server with `server.ssl_cert` and `server.ssl_key`.
- SSH tunnel for private access.

Client authentication uses `X-Auth-Token` during the WebSocket handshake. Avoid putting tokens in URLs.

## 7. Alert Logic Summary

The detector produces three main values:

- `estimated_usd`: estimated trade value in USDC.
- `underdog_score`: a 0-100 score for low-probability unusual activity.
- `alert_level`: `INFO`, `IMPORTANT`, or `SEVERE`.

Estimated trade value:

```text
estimated_usd = size * price
```

Hard filters:

- Trade value must be at least `large_trade_usd`, unless the short-window cumulative mode triggers.
- Outcome implied probability must be at most `underdog_max_implied_prob`.
- Order book depth must be at least `min_book_depth_usd`.
- Final `underdog_score` must be at least `underdog_score_threshold`.

Score:

```text
S = 0.30*A + 0.25*B + 0.20*C + 0.15*D + 0.10*E
```

Where:

- `A`: low implied probability score.
- `B`: relative weakness versus the favorite outcome.
- `C`: short-window upward price movement.
- `D`: volume anomaly versus recent median trade size.
- `E`: liquidity support from order book depth.

See [docs/ALGORITHM_EN.md](docs/ALGORITHM_EN.md) for details and examples.

## 8. Tests

```bash
cd server
source ../venv/bin/activate

python ../tests/test_detector.py
python ../tests/test_pipeline.py
python ../tests/test_connectivity.py
```

`test_detector.py` and `test_pipeline.py` can run without real credentials. `test_connectivity.py` contacts public Polymarket APIs.

## 9. Security Notes

- Use a strong random token.
- Use `wss://` for public deployments.
- Keep `server/config.yaml`, `client/config.json`, `.env`, database files, logs, certificates, and private keys out of Git.
- If a real token is ever pushed to a public repository, rotate it immediately.

## 10. Disclaimer

This project is for market monitoring and notification only. It is not betting, trading, investment, financial, or legal advice. Market data can be delayed, incomplete, or changed by upstream API behavior.
