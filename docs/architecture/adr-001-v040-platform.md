# ADR-001: TradingCLI 0.4 operational platform

## Status

Accepted

## Context

TradingCLI is a single-user, local-first paper-trading simulator backed by
SQLite. Version 0.4 adds accounting, execution simulation, research,
automation, interchange, encryption, and remote-access capabilities while
preserving the existing CLI, MCP server, and schema-v4 portfolios.

## Decision

Keep a modular monolith and add one schema-v5 migration. Use an append-only
double-entry ledger alongside the existing portfolio projection, explicit
provider and execution boundaries, persisted local automation, portable
authenticated encrypted snapshots, and an authenticated loopback-only HTTP
API.

## Rationale

The application does not need independent scaling or eventual consistency.
SQLite transactions provide the strongest and simplest correctness boundary.
Separate modules give the new capabilities test seams without forcing a
repository-pattern rewrite of the stable trading engine.

## Trade-offs

- The portfolio tables remain the operational projection; the ledger
  reconciles them rather than replacing every historical read path.
- Encryption protects portable snapshots. The live SQLite file remains
  owner-only unless a future SQLCipher backend is selected.
- Automation runs when explicitly invoked by a scheduler or long-running
  process; TradingCLI does not install an operating-system daemon.
- The HTTP API is loopback-only and bearer-authenticated. Multi-user network
  service is deferred.

## Consequences

Existing commands remain compatible. A single private pre-migration backup
protects schema-v4 data. Future extraction into services should be considered
only if multi-user scale, independent deployment, or broker-connected live
trading becomes a requirement.
