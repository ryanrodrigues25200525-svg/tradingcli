# tradingcli

A local, multi-account paper-trading CLI, live terminal dashboard, and MCP
server. It stores portfolio state in SQLite and uses Yahoo Finance market data.

Supported instruments include equities, ETFs, crypto, foreign exchange,
futures, and equity options. This is a simulation tool and does not place live
brokerage orders.

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
```

Start the MCP server over standard input/output:

```bash
python3 mcp_server.py
```

Portfolio data defaults to `~/.papertrade.db`. Set `PAPERTRADE_DB` to use a
different database path.

## Verify

The tests are standalone scripts:

```bash
python3 test_papertrade.py
python3 test_concurrency.py
python3 test_mcp_server.py
python3 test_dashboard.py
```

