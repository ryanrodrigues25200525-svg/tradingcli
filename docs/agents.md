# AI agents

TradingCLI ships a stdio MCP server, so an agent can trade against a local
simulated account instead of a hosted paper-trading API.

## A local alternative to Alpaca paper trading

The usual way to let an AI agent practise trading is a hosted paper API such as
Alpaca's: you sign up, mint keys, and every order the agent places is a network
call to someone else's server. TradingCLI does the same job with a local SQLite
file and a stdio MCP server.

| | Hosted paper API | TradingCLI |
| --- | --- | --- |
| **Getting started** | Account signup, API keys the agent must be trusted with | `pip install .` — no account, no keys, no secrets to leak |
| **Where state lives** | A vendor's servers | `~/.papertrade.db` on your disk, owner-only `0600` |
| **When the network dies** | The agent's run dies with it | Orders still fill; only fresh prices need the network |
| **Rate limits** | Yes — a fast agent loop hits them | None; it is a local database write |
| **Reproducibility** | Live prices, so a run can never be replayed exactly | Pin prices with `PAPERTRADE_PRICE_FILE` or `PAPERTRADE_STATIC_PRICES` and replay a run byte-for-byte |
| **Number of accounts** | Constrained by the provider | As many as you want, in one database |
| **Blast radius** | A misconfigured key can point at a live endpoint | There is no live order path in the codebase |

## The safety rails an agent gets

- **A deliberately narrow default surface.** The `core` profile exposes 63
  tools and leaves account deletion and reset out entirely. You opt into the
  destructive ones with `PAPERTRADE_MCP_PROFILE=advanced`.
- **One response contract.** Every core tool returns `{"ok":true,"data":...}`
  or `{"ok":false,"error":{...}}`, so an agent never has to parse prose.
- **Idempotency keys and agent attribution.** A retrying agent — or several
  agents in parallel MCP processes — cannot double-fill the same order.
- **Risk limits enforced in the engine, not in the prompt.** Daily loss,
  drawdown, per-symbol exposure and concentration caps reject the order rather
  than trusting the model to behave.
- **Realistic costs, so the agent does not learn on free fills.** Turn on
  commissions, slippage and liquidity participation with `execution set`.
- **A full audit trail.** Every fill lands in an append-only double-entry
  ledger you can reconcile, plus a journal with per-trade attribution.

> [!NOTE]
> **What this deliberately is not.** Yahoo Finance is not an exchange-grade
> feed: there is no order-book depth and no tick tape. Fills are modelled, not
> matched against a real queue. This is a place for an agent to learn a
> strategy and for you to audit its behaviour — moving anything to live capital
> is a separate, deliberate step that TradingCLI does not perform.

## Getting an agent trading

```bash
tradingcli-mcp                                  # core profile, 63 tools
PAPERTRADE_DB=./agent-sandbox.db tradingcli-mcp # give the agent its own database
```

Point your MCP client at that command. To keep an agent's experiments away from
your own portfolios, give it a separate `PAPERTRADE_DB` — the two never see
each other. When you want the results elsewhere, `tradingcli broker export
alpaca` writes Alpaca-shaped fill CSVs.

## Tool profiles

The `core` default covers order preview and lifecycle management, positions,
watchlists, market data, `portfolio_backtest`, health checks and backups.
Responses use one compact JSON contract:

```json
{"ok": true,  "data": {}}
{"ok": false, "error": {"code": "...", "message": "..."}}
```

Pick a broader catalog before starting the server if an agent needs one:

```bash
PAPERTRADE_MCP_PROFILE=advanced tradingcli-mcp  # 79 canonical tools
PAPERTRADE_MCP_PROFILE=full tradingcli-mcp      # all 86, legacy output
```

`advanced` adds destructive and specialist option/research/data operations.
`full` adds the seven old aliases (`buy`, `sell`, `cancel_order`,
`close_position`, `quote`, `trade_history` and `watchlist`) for existing
clients; `compat` is an alias for `full`. Set
`PAPERTRADE_MCP_RESPONSE_FORMAT=json|legacy` to override a profile's response
format. The `mcp_catalog` tool reports the active contract and full tool list.
