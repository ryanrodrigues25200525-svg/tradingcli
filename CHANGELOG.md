# Changelog

All notable changes are documented here.

## [0.4.1] - 2026-07-31

### Changed

- Reduced median startup latency by roughly one third by lazily importing
  network, HTTP, backup, encryption, and concurrency modules.
- Added a schema-version fast path that avoids replaying schema and trigger DDL
  on every command while retaining complete guard verification in `doctor`.
- Reused fresh prices across CLI processes through the SQLite market cache and
  increased the default quote TTL from 5 to 15 seconds.
- Made terminal quote, trade, snapshot, and FX commands avoid a second Yahoo
  metadata request when an indicative bid/ask is sufficient.
- Avoided redundant database-file permission updates on every connection.

### Fixed

- `doctor` now verifies the complete expected data-guard count rather than
  accepting any nonzero number of guards.

## [0.4.0] - 2026-07-31

### Added

- Append-only double-entry ledger, opening-balance migration, reconciliation,
  and symbol-level performance attribution.
- Commission, adverse-slippage, liquidity, and partial-fill simulation.
- Daily-loss, drawdown, symbol-exposure, and concentration risk limits.
- Backup inventory, restore safety backup, and retention workflows.
- Provider chaining with static-price failover and TTL caching.
- Chronological train/test SMA walk-forward research.
- Persistent automation jobs and execution history.
- Trade journal with tags, symbol/order links, and attachment hashes.
- Generic, Alpaca, and IBKR CSV import/export.
- Authenticated encrypted database snapshots and a bearer-authenticated,
  loopback-only HTTP API.
- Twelve new canonical MCP tools for operational features.

### Changed

- Schema version is now 5 and migrations backfill balanced opening ledgers.
- MCP catalogs now expose 63 core, 79 advanced, and 86 full tools.

## [0.3.0] - 2026-07-30

### Added

- Automatic private, transactionally consistent backups before schema upgrades.
- SQLite relationship, domain, and precision guards for portfolio state.
- Logical-integrity and fixed-precision details in `doctor`.
- Migration, rollback, corruption, future-schema, and precision contracts.

### Changed

- Monetary values use 2 decimal places, prices 6, and quantities 8 with
  deterministic decimal half-even normalization.
- Schema upgrades validate data before committing and refuse newer schemas.
- Trade, cashflow, corporate-action, order, and risk writes normalize values at
  storage boundaries.

## [0.2.0] - 2026-07-30

### Added

- Installable `tradingcli`, `tradingcli-dashboard`, and `tradingcli-mcp` entry points.
- Cash-preserving portfolio rebalance suggestions.
- Deterministic isolated test runner and GitHub Actions quality gate.
- Security, contribution, licensing, and release documentation.

### Changed

- Rebalance suggestions now exclude shorts, futures, and options.
- SQLite databases, sidecars, backup directories, and backups are owner-only.
- Health reports degrade when the schema version differs from the application.
- MCP internal errors return a correlation reference instead of implementation details.
- MCP core/advanced/full catalogs contain 55/67/74 tools.

### Security

- Database paths are no longer disclosed by health responses.
- MCP backup responses no longer reveal an absolute home-directory path.
