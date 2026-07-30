# Contributing

## Development setup

```bash
uv sync --locked --extra dev
```

## Quality checks

```bash
uv run ruff check .
uv run python run_tests.py
uv build
```

Tests are executable contract scripts and are run in separate subprocesses by
`run_tests.py`. New tests should be named `test_*.py`, avoid live network
traffic, use a temporary `PAPERTRADE_DB`, and finish with a nonzero exit status
when a contract fails.

Keep market-data adapters deterministic by stubbing `yfinance`. Never commit
real databases, backups, credentials, personal exports, or `.env` files.

## Pull requests

Describe the user-visible change, risk, and verification performed. Update the
README and changelog when commands, MCP catalogs, storage, or dependencies
change.
