# Security policy

## Supported versions

Security fixes are applied to the latest release on the `main` branch.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub's private
security-advisory feature for the repository owner. Include the affected
version, reproduction steps, impact, and any suggested mitigation.

## Local data

TradingCLI stores simulated account, order, position, watchlist, and audit data
in `~/.papertrade.db` by default. The application enforces owner-only `0600`
permissions on the database and SQLite sidecars. Backups are stored in
`~/.papertrade_backups`, with a `0700` directory and `0600` files.
Automatic pre-migration snapshots are stored in
`~/.papertrade.db.migrations` with the same owner-only permissions.

`security encrypt-copy` creates authenticated AES-GCM encrypted portable
snapshots using a password read from an environment variable. It does not
silently decrypt the live database or claim SQLCipher compatibility.

The optional HTTP API binds only to loopback addresses and requires a bearer
token of at least 24 characters. It is not a multi-user service, has no TLS
termination, and must not be exposed through a public reverse proxy without a
separate production authentication and transport-security layer.

Anyone able to run the CLI or MCP server as the same operating-system user can
access this data. The stdio MCP server is not an authenticated network service
and must not be exposed directly to untrusted users.

## Market-data privacy

Symbols in portfolios, pending orders, and watchlists may be sent to Yahoo
Finance through `yfinance` to retrieve prices, option chains, news, and history.
Do not use sensitive or confidential symbol universes without accepting that
third-party disclosure.

## Scope

TradingCLI is a paper-trading simulator. It does not place live brokerage
orders and must not be used as the sole source for financial decisions.
