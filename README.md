# tradingcli

A local, multi-account paper-trading CLI, live terminal dashboard, and MCP
server. It stores portfolio state in SQLite and uses Yahoo Finance market data.

Supported instruments include equities, ETFs, crypto, foreign exchange,
futures, and equity options. The schema-v3 engine also supports stop,
stop-limit, trailing-stop, bracket, OCO, OTO, and multi-leg option orders.
This is a simulation tool and does not place live brokerage orders.

## Setup

Requires Python 3.10 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Run

Launch the dashboard and first-run setup wizard:

```bash
python3 papertrade.py
```

Examples:

```bash
python3 papertrade.py accounts
python3 papertrade.py buy AAPL 5
python3 papertrade.py positions
python3 papertrade.py market
python3 papertrade.py backtest --lookback-days 730
```

Press `g` in the dashboard to open **Backtesting & Graphs**. It shows the
selected portfolio's live performance curve beside a `backtesting.py`
current-holdings backtest, including return, CAGR, volatility, Sharpe,
Sortino, costs, and maximum drawdown. The backtest universe is read directly
from that account's open positions in SQLite; another account's tickers are
never mixed in.

The backtest asks a specific retrospective question: how today's open
quantities and current cash would have performed if held unchanged over the
selected history. Because today's holdings are known in advance, it has
look-ahead and survivorship bias and is not an out-of-sample strategy test.
Adjusted daily Yahoo prices are used. Options are clearly reported as skipped
because reliable point-in-time option-chain history is unavailable; futures
use continuous series without roll costs.

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

Every command accepts one automation output flag: `--json`, `--csv`, or
`--quiet`. `--schema` returns the command tree without accessing market data,
and `doctor` checks database integrity and schema health.

Start the MCP server over standard input/output:

```bash
python3 mcp_server.py
```

The MCP server exposes 71 tools, including `portfolio_backtest`, the complete order lifecycle,
partial and full liquidation, option exercise/DNE and multi-leg strategies,
persistent watchlist CRUD, activities, calendar, market history, news,
screeners, crypto indications, FX rates, health checks, and backups. Mutating
tools accept agent attribution and idempotency keys for safe retries from
multiple independent MCP processes.

Portfolio data defaults to `~/.papertrade.db`. Set `PAPERTRADE_DB` to use a
different database path.

Yahoo Finance supplies the market data. Its historical quote/trade series and
crypto top-of-book output are explicitly marked aggregated or indicative;
Yahoo does not expose exchange tick tapes or full order-book depth.

## Verify

The tests are standalone scripts:

```bash
python3 test_papertrade.py
python3 test_backtesting.py
python3 test_concurrency.py
python3 test_mcp_server.py
python3 test_dashboard.py
python3 test_alpaca_parity.py
python3 test_cli.py
```
