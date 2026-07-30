# tradingcli

A local, multi-account paper-trading CLI, live terminal dashboard, and MCP
server. It stores portfolio state in SQLite and uses Yahoo Finance market data.

Supported instruments include equities, ETFs, crypto, foreign exchange,
futures, and equity options. The schema-v4 engine also supports stop,
stop-limit, trailing-stop, bracket, OCO, OTO, and multi-leg option orders.
This is a simulation tool and does not place live brokerage orders.

## Scope and safety

TradingCLI is a local paper-trading simulator. It never places live brokerage
orders, and its Yahoo Finance data is not an exchange-grade feed. Do not use it
as the sole source for financial decisions or expose its stdio MCP server as an
unauthenticated network service.

Fetching prices, news, history, and option chains sends held, pending, or
watchlisted symbols to Yahoo Finance through `yfinance`. See
[SECURITY.md](SECURITY.md) for the local-data and network privacy model.

## Install

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install .
tradingcli --version
```

For development:

```bash
uv sync --locked --extra dev
uv run python run_tests.py
```

`uv.lock` pins the complete cross-platform dependency graph. Use the locked
`uv` workflow for CI and repeatable deployments; the plain `pip` install above
is the lightweight end-user path.

## Run

Launch the dashboard and first-run setup wizard:

```bash
tradingcli
```

Examples:

```bash
tradingcli accounts
tradingcli buy AAPL 5
tradingcli positions
tradingcli market
tradingcli backtest --lookback-days 3650
```

Press `g` in the dashboard to open **Backtesting & Graphs**. It shows the
selected portfolio's live performance curve beside a `backtesting.py`
current-holdings backtest, including return, CAGR, volatility, Sharpe,
Sortino, costs, and maximum drawdown. The backtest universe is read directly
from that account's open positions in SQLite; another account's tickers are
never mixed in. Choose `6m`, `1y`, `2y`, `5y`, `10y`, `max`, or an exact
number of days when opening the view; pressing Enter requests five years.
Reported CAGR uses the actual elapsed calendar interval.

The backtest asks a specific retrospective question: how today's open
quantities and current cash would have performed if held unchanged over the
selected history. Because today's holdings are known in advance, it has
look-ahead and survivorship bias and is not an out-of-sample strategy test.
Adjusted daily Yahoo prices are used. Options are clearly reported as skipped
because reliable point-in-time option-chain history is unavailable; futures
use continuous series without roll costs.

Current-performance returns are time-weighted, so deposits and withdrawals do
not masquerade as trading gains or losses. Live marks and historical symbols
load concurrently, and the graph/backtest pair reuses overlapping history.

Alpaca-style order lifecycle:

```bash
python3 papertrade.py order submit AAPL --side buy --qty 10 --type limit --limit-price 185
python3 papertrade.py order submit AAPL --side sell --qty 10 --type trailing-stop --trail-percent 3
python3 papertrade.py order get --order-id 1
python3 papertrade.py order replace 1 --limit-price 184
python3 papertrade.py order cancel-all
python3 papertrade.py order submit AAPL --side buy --qty 10 --type limit --limit-price 180 --dry-run
python3 papertrade.py position close AAPL --percent 50
python3 papertrade.py position close-all
```

Bracket, OCO, and OTO exits use `--take-profit`, `--stop-loss`, and optionally
`--stop-loss-limit`. Orders support quantity or notional sizing, client order
IDs, time-in-force values, and eligible extended-hours limit orders.

Options, watchlists, and research:

```bash
python3 papertrade.py option get AAPL270115C00100000
python3 papertrade.py option exercise AAPL270115C00100000
python3 papertrade.py option do-not-exercise AAPL270115C00100000
python3 papertrade.py watchlist create Tech --symbols AAPL,MSFT,NVDA
python3 papertrade.py watchlist quotes Tech
python3 papertrade.py calendar --start 2026-07-01 --end 2026-07-31
python3 papertrade.py data bars AAPL --timeframe 1Day --limit 30
python3 papertrade.py data snapshot AAPL
python3 papertrade.py data movers
```

Portfolio rebalance suggestions are available through the MCP
`rebalance_suggest` tool. They preserve the account's existing cash allocation,
optimize only eligible long spot holdings, and explicitly skip shorts, futures,
and options. When anything is skipped, the response is marked `ok_partial` and
defines its weight scope explicitly. It is model output—not personalized
investment advice.

Every command accepts one automation output flag: `--json`, `--csv`, or
`--quiet`. `--schema` returns the command tree without accessing market data,
and `doctor` checks physical integrity, logical relationships, fixed-precision
storage, schema compatibility, and active database guards.

Start the MCP server over standard input/output:

```bash
python3 mcp_server.py
```

The server defaults to a focused 55-tool `core` catalog. It uses canonical
names, includes `portfolio_backtest`, order preview and lifecycle management,
positions, watchlists, market data, health checks, and backups, and leaves
destructive account deletion/reset out of the default agent surface. Core
responses use one compact JSON contract: `{"ok":true,"data":...}` or
`{"ok":false,"error":{"code":"...","message":"..."}}`.

Select a broader catalog before starting the server when an agent needs it:

```bash
PAPERTRADE_MCP_PROFILE=advanced tradingcli-mcp  # 67 canonical tools
PAPERTRADE_MCP_PROFILE=full tradingcli-mcp      # all 74, legacy output
```

`advanced` adds destructive and specialist option/research/data operations.
`full` adds the seven old aliases (`buy`, `sell`, `cancel_order`,
`close_position`, `quote`, `trade_history`, and `watchlist`) for existing
clients; `compat` is an alias for `full`. Set
`PAPERTRADE_MCP_RESPONSE_FORMAT=json|legacy` to override a profile's response
format. The `mcp_catalog` tool reports the active contract and complete tool
list.

Mutating tools accept agent attribution and idempotency keys for safe retries
from multiple independent MCP processes.

Portfolio data defaults to `~/.papertrade.db`. Database files and sidecars are
forced to owner-only `0600`; the backup directory is `0700` and backups are
`0600`. Set `PAPERTRADE_DB` to use a different database path. Set
`PAPERTRADE_MARKET_TIMEOUT` to change the default 15-second timeout used by
historical-data requests.

Schema v4 normalizes money to 2 decimal places, prices to 6, and quantities to
8 using decimal half-even rounding. SQLite guards reject invalid domains,
orphaned account/watchlist records, and values outside those precision
contracts—even when a caller bypasses the CLI.

Opening an older on-disk database upgrades it automatically. Before any
upgrade, TradingCLI writes a transactionally consistent snapshot beside the
database in `<database>.migrations/`; both the directory and snapshot remain
owner-only. A failed validation rolls back the migration and leaves the prior
schema version and data intact. Databases created by a newer TradingCLI release
are refused instead of being modified.

Yahoo Finance supplies the market data. Its historical quote/trade series and
crypto top-of-book output are explicitly marked aggregated or indicative;
Yahoo does not expose exchange tick tapes or full order-book depth.

## Verify

Run the isolated contract suite:

```bash
python3 run_tests.py
ruff check .
python3 -m build
```

The runner discovers every `test_*.py` contract and executes each one in an
isolated subprocess and temporary database. GitHub Actions runs the same checks
on Python 3.10 and 3.13.
