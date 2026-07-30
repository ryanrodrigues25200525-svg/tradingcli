# Changelog

All notable changes are documented here.

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
