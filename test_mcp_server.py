"""MCP contract smoke checks with no market-data network calls."""

import json
import os
import tempfile


os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")

import mcp_server as mcp


required = {
    "account_create",
    "account_details",
    "get_default_account",
    "buy",
    "sell",
    "preview_order",
    "risk_get",
    "risk_set",
    "audit_log",
    "asset_search",
    "validate_symbol",
    "bulk_quotes",
    "healthcheck",
    "database_backup",
    "trade_history",
    "export_history",
    "sync_corporate_actions",
    "market_status",
}
names = set(mcp.mcp._tool_manager._tools)
assert required <= names, required - names

created = mcp.account_create("agent", 10_000, idempotency_key="create-1", agent="codex")
assert created.startswith("created")
replayed = mcp.account_create(
    "agent", 10_000, idempotency_key="create-1", agent="codex"
)
assert "idempotent replay" in replayed

first = mcp.buy(
    "agent",
    "AAPL",
    1,
    limit=100,
    idempotency_key="order-1",
    agent="codex",
)
second = mcp.buy(
    "agent",
    "AAPL",
    1,
    limit=100,
    idempotency_key="order-1",
    agent="codex",
)
assert "pending" in first and "idempotent replay" in second
assert "allowed" in mcp.preview_order("agent", "AAPL", "buy", 1, price=100)
assert "risk agent" in mcp.risk_set("agent", allow_short=False, agent="codex")
assert json.loads(mcp.risk_get("agent"))["allow_short"] is False
assert json.loads(mcp.account_details("agent"))["pending_orders"] == 1
assert mcp.get_default_account() == "agent"
assert "source=codex" in mcp.orders("agent")
assert "[codex]" in mcp.audit_log("agent")
assert mcp.export_history("agent").startswith("id,timestamp")
health = json.loads(mcp.healthcheck())
assert health["status"] == "ok" and health["schema_version"] == 2
clock = json.loads(mcp.market_status())
assert clock["market"] == "NYSE" and clock["status"] in ("open", "closed")

print(f"MCP checks passed ({len(names)} tools)")
