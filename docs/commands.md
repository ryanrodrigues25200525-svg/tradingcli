# Command reference

Every command accepts one output flag — `--json`, `--csv` or `--quiet` — so it
can be scripted. `tradingcli --help-all` prints the full command tree and
`tradingcli --schema` returns it as data without touching market data.

- [Orders and positions](#orders-and-positions)
- [Options, watchlists and market data](#options-watchlists-and-market-data)
- [Ledger and reconciliation](#ledger-and-reconciliation)
- [Execution costs and risk limits](#execution-costs-and-risk-limits)
- [Research, journal and automation](#research-journal-and-automation)
- [Alerts, quotes and reports](#alerts-quotes-and-reports)
- [Backups and encryption](#backups-and-encryption)
- [Local HTTP API](#local-http-api)

## Orders and positions

Alpaca-style order lifecycle:

```bash
tradingcli order submit AAPL --side buy --qty 10 --type limit --limit-price 185
tradingcli order submit AAPL --side sell --qty 10 --type trailing-stop --trail-percent 3
tradingcli order get --order-id 1
tradingcli order replace 1 --limit-price 184
tradingcli order cancel-all
tradingcli order submit AAPL --side buy --qty 10 --type limit --limit-price 180 --dry-run
tradingcli position close AAPL --percent 50
tradingcli position close-all
```

Bracket, OCO and OTO exits use `--take-profit`, `--stop-loss` and optionally
`--stop-loss-limit`. Orders support quantity or notional sizing, client order
IDs, time-in-force values, and eligible extended-hours limit orders.

## Options, watchlists and market data

```bash
tradingcli option get AAPL270115C00100000
tradingcli option exercise AAPL270115C00100000
tradingcli option do-not-exercise AAPL270115C00100000
tradingcli watchlist create Tech --symbols AAPL,MSFT,NVDA
tradingcli watchlist quotes Tech
tradingcli calendar --start 2026-07-01 --end 2026-07-31
tradingcli data bars AAPL --timeframe 1Day --limit 30
tradingcli data snapshot AAPL
tradingcli data movers
```

Quote, latest-trade, snapshot and FX commands use a fast indicative quote path
where `bid` and `ask` equal the latest price. Library callers can request
Yahoo's slower metadata lookup with `latest_quote("AAPL", detailed=True)`.

Portfolio rebalance suggestions are available through the MCP
`rebalance_suggest` tool. They preserve the account's existing cash allocation,
optimize only eligible long spot holdings, and explicitly skip shorts, futures
and options. When anything is skipped the response is marked `ok_partial` and
defines its weight scope explicitly. It is model output — not personalized
investment advice.

## Ledger and reconciliation

An append-only double-entry ledger sits alongside the portfolio projection.
Funding, fills, commissions and migrated opening balances are all balanced
transactions; reconciliation can report or repair drift:

```bash
tradingcli ledger balances
tradingcli ledger reconcile
tradingcli ledger reconcile --repair
```

## Execution costs and risk limits

Execution simulation defaults to zero-cost, full-fill behaviour. Configure
commissions, adverse slippage, liquidity participation and a maximum fill size
per account:

```bash
tradingcli execution set --commission-bps 5 --slippage-bps 10 \
  --liquidity-fraction 0.5 --max-fill-quantity 100
tradingcli execution preview buy 250 185
```

Risk limits cover daily realized loss, peak-equity drawdown, per-symbol
exposure and concentration. They are enforced by the engine, so an order that
breaches one is rejected:

```bash
tradingcli risk --max-daily-loss 1000 --max-drawdown 0.10 \
  --max-symbol-exposure 25000 --max-concentration 0.30
```

## Research, journal and automation

```bash
tradingcli strategy walk-forward AAPL
tradingcli journal add "Earnings breakout" --tags setup,earnings --symbol AAPL
tradingcli journal attribution
tradingcli automation add nightly-backup backup --interval-seconds 86400
tradingcli automation run-due
tradingcli broker export alpaca
tradingcli broker import ibkr trades.csv
```

Automations are persisted and keep run history, but TradingCLI does not install
an operating-system daemon. Invoke `automation run-due` from cron, launchd or
another trusted scheduler. Beyond the example above, automations accept the
safe actions `quotes`, `alerts` and `report`.

## Alerts, quotes and reports

```bash
tradingcli quotes warm AAPL MSFT
tradingcli quotes stream AAPL MSFT --interval 5 --count 20 --check-alerts
tradingcli quotes daemon --interval 15 --check-alerts
tradingcli alert add aapl-breakout AAPL --above 225
tradingcli alert check --notify
tradingcli alert events
tradingcli report summary --save
tradingcli benchmark --iterations 20
tradingcli completion zsh > ~/.zfunc/_tradingcli
```

`quotes daemon` is a foreground local cache warmer; it never routes orders.
Alerts live in SQLite, and `--notify` also appends triggered events to the
owner-private `~/.papertrade_notifications.jsonl` inbox.

`doctor` checks physical integrity, logical relationships, fixed-precision
storage, schema compatibility and active database guards.

## Backups and encryption

Restoring requires `--yes` and first creates another consistent safety backup:

```bash
tradingcli backup list
tradingcli backup prune --keep 10
tradingcli backup restore /path/to/backup.db --yes
```

Authenticated encrypted snapshots protect portable database copies while the
live SQLite file keeps owner-only permissions:

```bash
PAPERTRADE_ENCRYPTION_PASSWORD='...' \
  tradingcli security encrypt-copy portfolio.db.enc
PAPERTRADE_ENCRYPTION_PASSWORD='...' \
  tradingcli security decrypt-copy portfolio.db.enc restored.db
```

## Local HTTP API

An authenticated, loopback-only HTTP API exposes health, accounts, positions,
journal entries and order previews. Tokens must be at least 24 characters:

```bash
PAPERTRADE_API_TOKEN='replace-with-a-long-random-token' tradingcli serve
```
