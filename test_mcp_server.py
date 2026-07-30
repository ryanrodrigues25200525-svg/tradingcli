"""MCP contract smoke checks with no market-data network calls."""

import json
import os
import sys
import tempfile


os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")
os.environ["PAPERTRADE_MCP_PROFILE"] = "full"
os.environ["PAPERTRADE_MCP_RESPONSE_FORMAT"] = "legacy"

import mcp_server as mcp

assert "pandas" not in sys.modules  # ordinary MCP tools avoid backtesting startup cost


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
    "portfolio_backtest",
    "rebalance_suggest",
    "healthcheck",
    "database_backup",
    "trade_history",
    "export_history",
    "sync_corporate_actions",
    "market_status",
    "order_submit",
    "order_get",
    "order_replace",
    "order_cancel",
    "order_cancel_all",
    "position_get",
    "position_close",
    "position_close_all",
    "option_contract",
    "option_exercise",
    "option_do_not_exercise",
    "option_multi_leg",
    "watchlist_create",
    "watchlist_list",
    "watchlist_get",
    "watchlist_add",
    "watchlist_remove",
    "watchlist_delete",
    "watchlist_quotes",
    "trading_calendar",
    "account_activity",
    "market_bars",
    "market_quotes",
    "market_trades",
    "market_latest_quote",
    "market_latest_trade",
    "market_snapshot",
    "market_news",
    "market_most_actives",
    "market_movers",
    "market_crypto_orderbook",
    "forex_rate",
    "mcp_catalog",
}
names = set(mcp.mcp._tool_manager._tools)
assert required <= names, required - names
assert len(names) == 74, names

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
backtest = json.loads(mcp.portfolio_backtest("agent"))
assert backtest["status"] == "no_positions" and backtest["account"] == "agent"
assert "[codex]" in mcp.audit_log("agent")
assert mcp.export_history("agent").startswith("id,timestamp")

advanced = mcp.order_submit(
    "agent",
    "MSFT",
    "buy",
    qty=1,
    order_type="stop-limit",
    limit_price=95,
    stop_price=100,
    client_order_id="advanced-1",
    agent="codex",
)
assert "pending" in advanced
advanced_order = json.loads(
    mcp.order_get(client_order_id="advanced-1", account="agent")
)
assert advanced_order["order_type"] == "stop_limit"
replacement = mcp.order_replace(
    advanced_order["id"], limit_price=96, client_order_id="advanced-2"
)
assert "replaced" in replacement
replacement_order = json.loads(
    mcp.order_get(client_order_id="advanced-2", account="agent")
)
assert "canceled" in mcp.order_cancel(replacement_order["id"], agent="codex")

assert "created watchlist" in mcp.watchlist_create(
    "agent", "Tech", "AAPL,MSFT", idempotency_key="watch-1", agent="codex"
)
assert json.loads(mcp.watchlist_get("agent", "Tech"))["symbols"] == [
    "AAPL",
    "MSFT",
]
assert "added" in mcp.watchlist_add("agent", "Tech", "NVDA")
assert "removed" in mcp.watchlist_remove("agent", "Tech", "NVDA")
assert json.loads(mcp.account_activity("agent"))
assert json.loads(mcp.trading_calendar("2026-07-01", "2026-07-06"))

health = json.loads(mcp.healthcheck())
assert health["status"] == "ok" and health["schema_version"] == 3
clock = json.loads(mcp.market_status())
assert clock["market"] == "NYSE" and clock["status"] in ("open", "closed")
catalog = json.loads(mcp.mcp_catalog())
assert catalog["profile"] == "full" and catalog["tool_count"] == 74

conn = mcp.pt.db()
with mcp.pt.writing(conn):
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("agent", "AAPL", 1, 90, 1, "spot", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("agent", "MSFT", 1, 190, 1, "spot", 0),
    )
conn.close()
original_live_price = mcp.pt.live_price
mcp.pt.live_price = lambda symbol: {"AAPL": 100, "MSFT": 200}[symbol]
try:
    assert "2 positions" in mcp.summary()
    mcp.pt.live_price = lambda _symbol: (_ for _ in ()).throw(
        SystemExit("quote unavailable")
    )
    assert "equity ~10,280.00" in mcp.summary()
finally:
    mcp.pt.live_price = original_live_price

print(f"MCP checks passed ({len(names)} tools)")
