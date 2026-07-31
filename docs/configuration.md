# Configuration

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `PAPERTRADE_DB` | SQLite portfolio path | `~/.papertrade.db` |
| `PAPERTRADE_MARKET_TIMEOUT` | Historical request timeout | `15` seconds |
| `PAPERTRADE_PRICE_CACHE_TTL` | Cross-process price cache life | `15` seconds |
| `PAPERTRADE_PRICE_PROVIDERS` | Ordered `file`, `static`, `yahoo` provider chain | `static,yahoo` |
| `PAPERTRADE_PRICE_FILE` | Deterministic local JSON price map | unset |
| `PAPERTRADE_STATIC_PRICES` | Inline JSON price map | `{}` |
| `PAPERTRADE_NOTIFICATION_FILE` | Alert notification JSONL inbox | `~/.papertrade_notifications.jsonl` |
| `PAPERTRADE_API_TOKEN` | Loopback API bearer token | unset |
| `PAPERTRADE_ENCRYPTION_PASSWORD` | Password used for encrypted copies | unset |
| `PAPERTRADE_MCP_PROFILE` | MCP catalog: `core`, `advanced`, `full` | `core` |
| `PAPERTRADE_MCP_RESPONSE_FORMAT` | Override a profile's response format: `json`, `legacy` | profile default |

## Deterministic prices

Static or file-backed prices can precede Yahoo, either for reproducible
simulations or as failover:

```bash
PAPERTRADE_PRICE_PROVIDERS=file,static,yahoo
PAPERTRADE_PRICE_FILE=./simulation-prices.json
PAPERTRADE_STATIC_PRICES='{"AAPL":185.25}'
```

Fresh Yahoo prices are cached in SQLite, so separate terminal invocations reuse
them without another network round trip. Set `PAPERTRADE_PRICE_CACHE_TTL=0`
when every command must fetch.

## Storage and permissions

Portfolio data defaults to `~/.papertrade.db`. Database files and sidecars are
forced to owner-only `0600`; the backup directory is `0700` and backups are
`0600`.

Schema v6 normalizes money to 2 decimal places, prices to 6 and quantities to 8
using decimal half-even rounding. SQLite guards reject invalid domains,
orphaned account/watchlist records, and values outside those precision
contracts — even when a caller bypasses the CLI.

## Migrations

Opening an older on-disk database upgrades it automatically. Before any
upgrade, TradingCLI writes a transactionally consistent snapshot beside the
database in `<database>.migrations/`; both the directory and snapshot remain
owner-only. A failed validation rolls back the migration and leaves the prior
schema version and data intact. Databases created by a newer TradingCLI release
are refused rather than modified.

## Market data

Yahoo Finance supplies the market data. Its historical quote/trade series and
crypto top-of-book output are explicitly marked aggregated or indicative; Yahoo
does not expose exchange tick tapes or full order-book depth.
