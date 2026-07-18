"""Exercise the full MCP catalog with deterministic engine/provider adapters."""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile


tmp = tempfile.TemporaryDirectory()
os.environ["PAPERTRADE_DB"] = str(Path(tmp.name) / "mcp-features.db")
os.environ["PAPERTRADE_MCP_PROFILE"] = "full"
os.environ["PAPERTRADE_MCP_RESPONSE_FORMAT"] = "legacy"

import mcp_server as server  # noqa: E402


pt = server.pt
originals = {
    name: getattr(pt, name)
    for name in (
        "live_price",
        "pnl",
        "account_performance",
        "show_chain",
        "search_assets",
        "validate_asset",
        "backup_database",
        "sync_corporate_actions",
        "close_position",
        "close_all_positions",
        "get_position",
        "option_contract_details",
        "exercise_option",
        "do_not_exercise_option",
        "submit_option_multileg",
        "watchlist_quotes",
        "market_history",
        "latest_quote",
        "latest_trade",
        "market_snapshot",
        "market_news",
        "market_screener",
        "crypto_orderbook",
    )
}

pt.live_price = lambda _symbol: 100.0
pt.pnl = lambda _conn, account: print(f"pnl {account}")
pt.account_performance = lambda *_args, **_kwargs: (
    [("2026-01-01", 10_000), ("2026-01-02", 10_100)],
    {
        "start": "2026-01-01",
        "end": "2026-01-02",
        "days": 1,
        "start_eq": 10_000,
        "end_eq": 10_100,
        "total": 0.01,
        "cagr": 1.0,
        "sharpe": 1.2,
        "sortino": 1.3,
        "vol": 0.2,
        "mdd": -0.01,
        "best": 0.01,
        "worst": -0.005,
    },
)
pt.show_chain = lambda underlying, expiry=None: print(
    f"chain {underlying} {expiry or 'expiries'}"
)
pt.search_assets = lambda query, limit=8: [
    {"symbol": "AAPL", "name": query, "limit": limit}
]
pt.validate_asset = lambda symbol: {"valid": True, "symbol": symbol.upper()}
pt.backup_database = lambda _conn: str(Path(tmp.name) / "backup.db")
pt.sync_corporate_actions = lambda _conn, account=None, source="mcp": print(
    f"synced {account or 'all'} {source}"
)
pt.close_position = lambda _conn, account, symbol, *args, **kwargs: print(
    f"closed {account} {symbol}"
)
pt.close_all_positions = lambda _conn, account, **kwargs: print(f"closed all {account}")
pt.get_position = lambda _conn, account, symbol: {
    "account": account,
    "symbol": symbol,
    "signed_qty": 1,
}
pt.option_contract_details = lambda symbol: {"symbol": symbol, "strike_price": 100}
pt.exercise_option = lambda _conn, account, symbol, *args, **kwargs: print(
    f"exercised {account} {symbol}"
)
pt.do_not_exercise_option = lambda _conn, account, symbol, **kwargs: print(
    f"do not exercise {account} {symbol}"
)
pt.submit_option_multileg = lambda _conn, account, legs, *args, **kwargs: print(
    f"multi-leg {account} {len(legs)}"
)
pt.watchlist_quotes = lambda _conn, account, watchlist: {
    "account": account,
    "name": watchlist,
    "quotes": [{"symbol": "AAPL", "price": 100}],
}
pt.market_history = lambda symbol, kind, start, end, timeframe, limit: {
    "symbol": symbol,
    "kind": kind,
    "timeframe": timeframe,
    "data": [{"close": 100, "limit": limit}],
}
pt.latest_quote = lambda symbol: {
    "symbol": symbol,
    "bid": 99,
    "ask": 101,
    "last": 100,
}
pt.latest_trade = lambda symbol: {"symbol": symbol, "price": 100}
pt.market_snapshot = lambda symbol: {"symbol": symbol, "change": 1}
pt.market_news = lambda symbol, limit=10: [{"symbol": symbol, "limit": limit}]
pt.market_screener = lambda name, limit=20: [{"symbol": "AAPL", "screen": name}]
pt.crypto_orderbook = lambda symbol: {"symbol": symbol, "indicative": True}

try:

    async def asynchronous_probe():
        return {"catalog": "ok"}

    async_tool = SimpleNamespace(fn=asynchronous_probe)
    server._wrap_json_tool(async_tool)
    assert json.loads(asyncio.run(async_tool.fn()))["data"]["catalog"] == "ok"

    assert server.account_create("one", 10_000).startswith("created")
    assert server.account_create("temporary", 1_000).startswith("created")
    assert "one:" in server.account_list()
    assert "default account" in server.set_default_account("one")
    assert "deposited" in server.deposit("one", 500)
    assert "withdrew" in server.withdraw("one", 100)
    assert "must be positive" in server.deposit("one", 0)
    assert "must be positive" in server.withdraw("one", -1)
    assert json.loads(server.risk_get("one"))["allow_short"] is True
    assert "risk one" in server.risk_set(
        "one", allow_short=True, allow_naked_options=True, max_gross_leverage=5
    )
    assert json.loads(server.preview_order("one", "AAPL", "buy", 1, 100))["allowed"]

    assert "pending" in server.buy("one", "AAPL", 1, limit=90)
    assert "pending" in server.sell("one", "MSFT", 1, limit=120)
    assert "AAPL" in server.orders("one") and "AAPL" in server.trade_history("one")
    assert "pnl one" in server.pnl("one")
    assert "return +1.00%" in server.performance("one")
    assert "chain AAPL" in server.option_chain("AAPL")
    assert "ES=F" in server.futures_symbols()
    assert "closed one AAPL" in server.close_position("one", "AAPL")
    assert "closed one AAPL" in server.position_close("one", "AAPL", percent=50)
    assert "closed all one" in server.position_close_all("one")
    assert json.loads(server.position_get("one", "AAPL"))["signed_qty"] == 1

    assert "pending" in server.buy_option(
        "one", "AAPL", "2027-01-15", 100, "C", 1, limit=2
    )
    assert "pending" in server.sell_option(
        "one", "AAPL", "2027-01-15", 110, "C", 1, limit=2
    )
    occ = "AAPL270115C00100000"
    assert json.loads(server.option_contract(occ))["strike_price"] == 100
    assert "exercised" in server.option_exercise("one", occ)
    assert "do not exercise" in server.option_do_not_exercise("one", occ)
    assert "invalid legs JSON" in server.option_multi_leg("one", "not-json")
    assert "multi-leg one 2" in server.option_multi_leg(
        "one",
        json.dumps(
            [
                {"symbol": occ, "side": "buy", "qty": 1},
                {"symbol": "AAPL270115C00110000", "side": "sell", "qty": 1},
            ]
        ),
    )

    assert "AAPL: 100.00" in server.quote("AAPL")
    assert "symbol required" in server.quote(" ")
    assert "AAPL: 100.00" in server.watchlist("AAPL")
    assert len(json.loads(server.bulk_quotes("AAPL,MSFT"))) == 2
    assert "no symbols" in server.bulk_quotes("")
    assert json.loads(server.asset_search("apple"))[0]["symbol"] == "AAPL"
    assert json.loads(server.validate_symbol("aapl"))["valid"]
    assert server.database_backup().endswith("backup.db")
    assert "synced one" in server.sync_corporate_actions("one")

    assert "created watchlist" in server.watchlist_create("one", "Tech", "AAPL")
    assert json.loads(server.watchlist_list("one"))[0]["name"] == "Tech"
    assert json.loads(server.watchlist_get("one", "Tech"))["symbols"] == ["AAPL"]
    assert "added" in server.watchlist_add("one", "Tech", "MSFT")
    assert "removed" in server.watchlist_remove("one", "Tech", "MSFT")
    assert json.loads(server.watchlist_quotes("one", "Tech"))["quotes"]
    assert "deleted" in server.watchlist_delete("one", "Tech")

    assert json.loads(server.market_bars("AAPL"))["kind"] == "bars"
    assert json.loads(server.market_quotes("AAPL"))["kind"] == "quotes"
    assert json.loads(server.market_trades("AAPL"))["kind"] == "trades"
    assert json.loads(server.market_latest_quote("AAPL"))["last"] == 100
    assert json.loads(server.market_latest_trade("AAPL"))["price"] == 100
    assert json.loads(server.market_snapshot("AAPL"))["change"] == 1
    assert json.loads(server.market_news("AAPL"))[0]["limit"] == 10
    assert json.loads(server.market_most_actives())[0]["screen"] == "most_actives"
    movers = json.loads(server.market_movers())
    assert movers["gainers"][0]["screen"] == "day_gainers"
    assert json.loads(server.market_crypto_orderbook("BTC-USD"))["indicative"]
    assert json.loads(server.forex_rate("USD/EUR"))["symbol"] == "USDEUR=X"
    assert "must look like" in server.forex_rate("USD")

    assert "canceled" in server.order_cancel_all("one")
    assert "no pending orders" in server.tick()
    assert "no positions" in server.positions("one")
    assert "renamed" in server.rename_account("one", "renamed")
    assert server.get_default_account() == "renamed"
    assert json.loads(server.account_details("renamed"))["account"] == "renamed"
    assert "reset" in server.reset_account("renamed", 5_000)
    assert "deleted" in server.delete_account("temporary")
finally:
    for name, original in originals.items():
        setattr(pt, name, original)
    tmp.cleanup()

print("MCP feature checks passed")
