# tradingcli — agent guide

> `tradingcli` is a **local, offline-first paper trading simulator**. It never places real brokerage orders. All state lives in a single SQLite file (default `~/.papertrade.db`). Market data comes from Yahoo Finance (`yfinance`). Keep that contract in mind when automating: respect the DB, respect rate limits.

## Quick orientation

| File | Role |
|---|---|
| `papertrade.py` | CLI + library. Every command is a function taking `(conn, ...)` — import and call directly for fastest agent use. |
| `mcp_server.py` | MCP server (54 core / 66 advanced / 73 full tools). Preferred agent entry point over raw CLI. |
| `dashboard.py` | Rich TUI (`tradingcli dash`). Not useful headless. |
| `web_ui.py` | FastAPI REST + static SPA (`/api/*`, `static/index.html`). |
| `portfolio_backtest.py` | `backtesting.py` engine for current-holdings retrospective. |
| `test_*.py` | Standalone scripts (`python3 test_papertrade.py`), not `pytest`. |
| `static/index.html` | Single-page frontend (Chart.js). Served by `web_ui.py`. |

## Environment

- **DB** `PAPERTRADE_DB=:memory:` works for isolated agent tests (in-memory SQLite, no file). `PAPERTRADE_DB=/tmp/x.db` isolates per-agent runs. DB path is re-read on each `pt.db()` call so env changes after import are respected.

- **Python** `>=3.10`. Install: `pip install -r requirements.txt` or `pip install -e .` / `uv tool install .`.
- **DB** `PAPERTRADE_DB` env var overrides `~/.papertrade.db`. Tests use `tempfile` isolation — never touch the real DB in tests.
- **SQLite** WAL + `busy_timeout=10s` + `BEGIN IMMEDIATE` (`writing()` context). Multiple agents may share the DB. **Always use `writing(conn)` for mutations.**

## The right tool for the job

| Need | Use | Why |
|---|---|---|
| Agent-driven trading / research | **MCP** (`mcp_server.py`) | 73 typed tools, idempotency keys, single DB contract, no shell parsing. |
| Script / batch / CI | **Import `papertrade` as library** | `conn = pt.db(); pt.place(conn, ...)` — faster + structured errors. |
| Ad-hoc human use | **CLI** (`tradingcli` or `python3 papertrade.py`) | `--json` / `--csv` / `--quiet` for automation flags. |
| Charts / web | **REST** (`web_ui.py` → `GET /api/positions`, etc.) | `uvicorn web_ui:app --port 8080`. |

Prefer MCP or library import over shelling out to the CLI. The CLI's `--json` mode is for cases where you must shell out.

## MCP — the intended agent surface

```bash
# core (54 tools) — default, safe for autonomous agents
python3 mcp_server.py

# advanced (66) — adds destructive + specialist option/research ops
PAPERTRADE_MCP_PROFILE=advanced python3 mcp_server.py

# full (73) — adds 7 legacy aliases (buy/sell/quote/…) for compat
PAPERTRADE_MCP_PROFILE=full python3 mcp_server.py

# response format override
PAPERTRADE_MCP_RESPONSE_FORMAT=json  # or legacy
```

- **Core contract:** `{"ok": true, "data": …}` / `{"ok": false, "error": {"code": "…", "message": "…"}}` when `PAPERTRADE_MCP_RESPONSE_FORMAT=json`.
- **Profiles:** `mcp_catalog` reports active profile + full tool list.
- **Idempotency:** Mutating tools take `idempotency_key` + `agent` for safe retries across independent MCP processes.

See `.mcp.json` for a Claude Code / Cursor / Hermes config example.

## CLI as library (fastest for agents)

```python
import papertrade as pt

conn = pt.db()
pt.create_account(conn, "agent_test", 100_000, source="my-agent", request_id="seed-1")
# Stubbing Yahoo for deterministic tests: patch `pt.live_price` and pass `price_fn` explicitly where needed — defaults are lazy so monkey-patching works: `pt.live_price = lambda s: 100.0; pt.place(conn, ..., price_fn=pt.live_price)`
acct = pt.resolve_account(conn, None)  # falls back to default_account

# trade — all writes are atomic (BEGIN IMMEDIATE inside)
pt.place(conn, acct, "AAPL", "buy", 10, None, source="my-agent", request_id="buy-1")

# richer order lifecycle
pt.submit_order(conn, acct, "AAPL", "buy", 10, order_type="limit",
                limit_price=180, source="my-agent", request_id="limit-1")
pt.preview_order(conn, acct, "AAPL", "buy", 10, price=182.5)  # dry-run risk check

# positions / orders / pnl / perf
pt.list_positions(conn, acct)
pt.get_position(conn, acct, "AAPL")
pt.close_position(conn, acct, "AAPL", percent=50, source="my-agent")
pt.healthcheck(conn)
pt.backup_database(conn)
```

- **Errors** are raised as `SystemExit(message)` — catch and surface `str(exc)`.
- **Idempotency** `source + request_id` dedupes exact replays and rejects key reuse for different intents.
- **Risk** `pt.risk_limits(conn, acct)` / `pt.set_risk_limits(...)` controls shorting / naked options / leverage / notional caps.

## Automation flags (CLI)

Every CLI command accepts one of `--json` / `--csv` / `--quiet` and `--schema` (prints `CLI_COMMAND_TREE` without touching market data):

```bash
python3 papertrade.py accounts --json
python3 papertrade.py positions --json | jq .
python3 papertrade.py --schema | jq .order
python3 papertrade.py doctor --json
```

`--help-all` expands subcommand help; `PAPERTRADE_DB=/tmp/test.db` isolates runs.

## Instruments & order types

- **Spot** `AAPL`, `BTC-USD`, `EURUSD=X` — mult 1, cash-funded.
- **Futures** `ES=F`, `MES=F`, `GC=F`, … (`FUTURES` dict) — contract mult + margin.
- **Options** OCC `AAPL260116C00250000` (built via `pt.build_occ(root, expiry, strike, cp)`), mult 100.
- **Order types** `market | limit | stop | stop_limit | trailing_stop` (+ `bracket | oco | oto | mleg` via `order_class`).
- **TIF** `gtc | day | ioc | fok | opg | cls`; bracket/OCO/OTO use `take_profit` / `stop_loss` (+ `stop_loss_limit`).

## Common recipes

```bash
# New account + trade + inspect
python3 papertrade.py new mybook --cash 50000
python3 papertrade.py buy AAPL 10 -a mybook
python3 papertrade.py positions -a mybook --json

# Alpaca-style limit + cancel lifecycle
python3 papertrade.py order submit AAPL --side buy --qty 10 --type limit --limit-price 180 -a mybook
python3 papertrade.py order list -a mybook --status pending
python3 papertrade.py order cancel 1

# Backtest current holdings (5y lookback, 10 bps commission)
python3 papertrade.py backtest -a mybook --lookback-days 1825 --commission-bps 10

# Web UI
python3 web_ui.py              # :8080  (or: uvicorn web_ui:app --port 8080)
curl http://localhost:8080/api/accounts
curl http://localhost:8080/api/positions?account=mybook
```

## Verification

```bash
python3 test_papertrade.py
python3 test_mcp_server.py
python3 test_mcp_profiles.py
python3 test_backtesting.py
python3 test_dashboard.py
# plus test_alpaca_parity / test_cli / test_concurrency / test_invariants / test_market_data / test_mcp_features
```


## New in 0.4 — agent realism & streaming

- **Config** `tradingcli config list/get/set/delete` and `GET /api/config/*` + MCP `config_*` — generic key/value store for agent preferences.
- **Events** `tradingcli events --since-id 123` and `GET /api/events?since_id=&limit=` + `GET /api/events/stream` (SSE) + MCP `events` — replaces polling `tick`/`audit` for incremental sync. `since_id` is the last `audit_log.id` you saw.
- **Export/Import** `tradingcli export -a NAME --format parquet --output x.parquet` and `GET /api/export?format=parquet` + `POST /api/import` + `import_history` lib — for quant restore. CSV remains default.
- **Benchmark** `tradingcli perf -a NAME --benchmark SPY` and `GET /api/performance?benchmark=SPY` — shows `alpha` vs benchmark. `portfolio_backtest` also supports `benchmark_history("SPY", start, end)`.
- **Realism** `tradingcli risk -a NAME --borrow-bps 300 --commission-bps 10 --slippage-bps 5 --allow-fractional/--no-allow-fractional` — stored in `risk_settings`, applied in `_fill_locked` as extra cost/slippage and fractional check.
- **Streaming quotes** MCP `quotes_stream(symbols, snapshots=3, interval_sec=1)` — batched `batch_prices` snapshots without WebSocket. Web `WS /ws` pushes health every 5s and `GET /api/events/stream` pushes audit events.

All new MCP tools are in `core` (now 60 tools) — no profile change needed for agents.

## Pitfalls

- **Don't import `papertrade` and then shell out to `papertrade.py` in the same flow** — you'll double-open the DB. Pick one surface.
- **Yahoo is rate-limited and aggregated.** Historical quotes/trades are marked as such; crypto order-book is indicative. Don't claim tick-level fidelity.
- **Options history is stubbed** in backtests (reported as `skipped`).
- **Corporate actions** need an explicit `sync_corporate_actions` / `pt.sync_corporate_actions(conn, account)` — they are not auto-applied.
- **`.papertrade.db*` is gitignored.** Backups go to `~/.papertrade_backups/`.
