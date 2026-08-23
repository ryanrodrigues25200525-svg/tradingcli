# tradingcli

Local, multi-account paper-trading CLI, live terminal dashboard, REST + MCP server. SQLite state, Yahoo Finance market data. Simulation only — never places live brokerage orders.

Stocks / ETFs / crypto (`BTC-USD`) / FX (`EURUSD=X`) / futures (`ES=F`) / equity options (OCC `AAPL260116C00250000`). Engine supports `market / limit / stop / stop_limit / trailing_stop` plus `bracket / OCO / OTO / mleg` with `take_profit / stop_loss`, notional sizing, client order IDs, TIF (`gtc / day / ioc / fok / opg / cls`), and extended-hours limit orders.

## Setup

Requires Python 3.10+.

```bash
# from source
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # or: pip install -e .

# isolated tool install (uv)
uv tool install .
```

Database defaults to `~/.papertrade.db`; override with `PAPERTRADE_DB=/tmp/test.db` or `PAPERTRADE_DB=:memory:` for isolated in-memory tests.

## Run

```bash
# first-run wizard + live TUI dashboard
python3 papertrade.py            # or: tradingcli
# explicit dash
python3 papertrade.py dash       # or: tradingcli dash
```

```bash
# accounts
python3 papertrade.py new mybook --cash 50000
python3 papertrade.py accounts --json
python3 papertrade.py use mybook

# simple trade → inspect
python3 papertrade.py buy AAPL 5 -a mybook
python3 papertrade.py positions -a mybook --json
python3 papertrade.py market --json

# Alpaca-style order lifecycle
python3 papertrade.py order submit AAPL --side buy --qty 10 --type limit --limit-price 185 -a mybook
python3 papertrade.py order submit AAPL --side sell --qty 10 --type trailing-stop --trail-percent 3 -a mybook
python3 papertrade.py order get --order-id 1 --json
python3 papertrade.py order replace 1 --limit-price 184
python3 papertrade.py order cancel-all -a mybook
python3 papertrade.py order submit AAPL --side buy --qty 10 --type limit --limit-price 180 --dry-run --json
python3 papertrade.py position close AAPL --percent 50 -a mybook
python3 papertrade.py position close-all -a mybook

# bracket / OCO / OTO — use --take-profit / --stop-loss / --stop-loss-limit
# options / chain / watchlists / research
python3 papertrade.py option get AAPL270115C00100000 --json
python3 papertrade.py option exercise AAPL270115C00100000 -a mybook
python3 papertrade.py chain AAPL 2027-01-15
python3 papertrade.py watchlist create Tech --symbols AAPL,MSFT,NVDA -a mybook
python3 papertrade.py watchlist quotes Tech -a mybook --json
python3 papertrade.py data bars AAPL --timeframe 1Day --limit 30 --json
python3 papertrade.py data snapshot AAPL --json
python3 papertrade.py data movers --json
python3 papertrade.py calendar --start 2026-07-01 --end 2026-07-31 --json

# backtest current holdings as a retrospective (see notes below)
python3 papertrade.py backtest -a mybook --lookback-days 1825 --commission-bps 10 --json
```

Every command accepts one automation flag: `--json` / `--csv` / `--quiet`. `--schema` prints the command tree without touching market data; `doctor` checks DB integrity.

Press `g` in the TUI to open **Backtesting & Graphs** — live equity curve beside a `backtesting.py` current-holdings backtest (return, CAGR, vol, Sharpe, Sortino, costs, maxDD). Universe is the selected account's open positions; `6m / 1y / 2y / 5y / 10y / max` presets or exact days. CAGR uses real calendar elapsed time; options appear as `skipped` (no point-in-time chain history); futures use continuous series without roll costs. Curves are time-weighted, so deposits/withdrawals don't masquerade as alpha. The result has intentional look-ahead/survivorship bias — it's a "what if we held today's book" retrospective, not an OOS strategy test. Yahoo adjusted closes are used; crypto top-of-book is indicative.

## REST + web UI

```bash
python3 web_ui.py                    # http://127.0.0.1:8080  (or: tradingcli-web) — localhost only, no auth
uvicorn web_ui:app --port 8080       # alternative
HOST=0.0.0.0 PORT=3000 python3 web_ui.py  # expose to network (no auth — do not do this on untrusted networks)
```

- `GET /` → SPA (`static/index.html`, Chart.js). Tabs: Portfolio / Orders / Watchlists / Backtest / Settings.
- Binds to `127.0.0.1` by default; mutating routes have no auth — do not expose to the network. CORS is limited to `localhost:8080`/`3000`.
- `GET /api/accounts`, `/api/positions?account=…`, `/api/orders?account=…`, `/api/watchlists`, `/api/backtest?account=…&days=365`, `/api/equity-curve`, `/api/market/status`, `/api/market/quote/{symbol}`, `/api/market/history/{symbol}`, `/api/health`, `/api/config`. All return `{"ok": true, "data": …}` or `{"ok": false, "error": "…"}`.

## MCP server (for Claude Code / Cursor / Hermes)

Preferred surface for AI agents — typed, idempotent, single DB contract.

```bash
python3 mcp_server.py                              # core — 54 tools (default, safe)
PAPERTRADE_MCP_PROFILE=advanced python3 mcp_server.py  # 66 tools — adds destructive/specialist ops
PAPERTRADE_MCP_PROFILE=full python3 mcp_server.py      # 73 tools — adds 7 legacy aliases (buy/sell/quote/…)
PAPERTRADE_MCP_RESPONSE_FORMAT=json python3 mcp_server.py  # force JSON contract
# or via entry point after pip install -e .
tradingcli-mcp
```

`core` responses use `{"ok": true, "data": …}` / `{"ok": false, "error": {"code": "…", "message": "…"}}`. `mcp_catalog` reports the active profile + tool list. Mutating tools take `idempotency_key` + `agent` for safe retries.

**Client config** — copy `.mcp.json` to your client's MCP config and fix the path:

```json
{
  "mcpServers": {
    "papertrade": {
      "command": "python3",
      "args": ["/absolute/path/to/tradingcli/mcp_server.py"],
      "env": { "PAPERTRADE_MCP_PROFILE": "core" }
    }
  }
}
```

For agents importing as a library instead of MCP, see `docs/agent-guide.md`.

## Verify

Tests are standalone scripts (no `pytest` needed):

```bash
python3 test_papertrade.py
python3 test_backtesting.py
python3 test_performance.py
python3 test_concurrency.py
python3 test_mcp_server.py
python3 test_mcp_profiles.py
python3 test_dashboard.py
python3 test_alpaca_parity.py
python3 test_cli.py
python3 test_edge_cases.py
python3 test_market_data.py
python3 test_mcp_features.py
python3 test_invariants.py
```


```bash
tradingcli config list
tradingcli config set myapp.threshold 42
tradingcli events --since-id 0 --limit 20 --json
tradingcli export -a mybook --format parquet --output mybook.parquet
tradingcli perf -a mybook --benchmark SPY --json
```

Web streaming: `GET /api/events/stream?since_id=0` (SSE) and `WS /ws` for live health.

Yahoo Finance supplies market data. Historical quote/trade series and crypto top-of-book are explicitly marked aggregated/indicative — not exchange tick tapes or full depth.
