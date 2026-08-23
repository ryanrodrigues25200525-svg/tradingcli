# Note for Claude / AI agents

This project is a local paper-trading simulator. Prefer the MCP server or
importing `papertrade` as a library over shelling out to the CLI.

- **MCP (preferred):** `python3 mcp_server.py` (env `PAPERTRADE_MCP_PROFILE=core|advanced|full`). See `docs/agent-guide.md` and `.mcp.json`.
- **Library:** `import papertrade as pt; conn = pt.db(); pt.place(conn, ...)` — errors are `SystemExit`, use `source`+`request_id` for idempotency, `writing(conn)` for mutations.
- **CLI automation:** `python3 papertrade.py --json / --csv / --quiet / --schema`; `PAPERTRADE_DB` isolates the DB.
- **Docs:** `docs/agent-guide.md` is the full guide. `README.md` is the human entry point.

Do not create AGENTS.md / CLAUDE.md at repo root — the Hermes guard blocks them. Use `docs/agent-guide.md`.
