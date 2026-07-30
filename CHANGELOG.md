# Changelog

All notable changes are documented here.

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
