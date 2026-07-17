"""Deterministic Yahoo-adapter checks; all provider traffic is stubbed."""

import contextlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile

import pandas as pd
import yfinance as yf


tmp = tempfile.TemporaryDirectory()
os.environ["PAPERTRADE_DB"] = str(Path(tmp.name) / "market-data.db")

import dashboard  # noqa: E402
import papertrade as pt  # noqa: E402
import portfolio_backtest as pbt  # noqa: E402


index = pd.to_datetime(["2026-07-15", "2026-07-16"], utc=True)
history = pd.DataFrame(
    {
        "Open": [99.0, 100.0],
        "High": [102.0, 103.0],
        "Low": [98.0, 99.0],
        "Close": [100.0, 101.0],
        "Volume": [1_000, 1_200],
        "Dividends": [0.0, 0.0],
        "Stock Splits": [0.0, 0.0],
    },
    index=index,
)
calls = pd.DataFrame(
    {
        "strike": [100.0, 110.0],
        "bid": [4.0, 1.0],
        "ask": [6.0, 3.0],
        "lastPrice": [5.0, 2.0],
    }
)
puts = pd.DataFrame(
    {
        "strike": [100.0, 110.0],
        "bid": [3.0, 8.0],
        "ask": [5.0, 10.0],
        "lastPrice": [4.0, 9.0],
    }
)


class FakeTicker:
    def __init__(self, symbol):
        self.symbol = symbol
        self.fast_info = {"lastPrice": 101.0, "previousClose": 100.0}
        self.info = {"bid": 100.5, "ask": 101.5}
        self.options = ["2027-01-15"]
        self.news = [
            {
                "content": {
                    "title": "Example headline",
                    "provider": {"displayName": "Example Wire"},
                    "pubDate": "2026-07-16T12:00:00Z",
                    "canonicalUrl": {"url": "https://example.test/story"},
                }
            }
        ]

    def history(self, **_kwargs):
        return history.copy()

    def option_chain(self, _expiry):
        return SimpleNamespace(calls=calls.copy(), puts=puts.copy())


class FakeSearch:
    def __init__(self, _query, max_results, news_count):
        assert max_results >= 1 and news_count == 0
        self.quotes = [
            {
                "symbol": "AAPL",
                "shortname": "Apple Inc.",
                "quoteType": "EQUITY",
                "exchange": "NMS",
            }
        ]


original_ticker, original_search, original_screen = yf.Ticker, yf.Search, yf.screen
yf.Ticker = FakeTicker
yf.Search = FakeSearch
yf.screen = lambda name, count: {
    "quotes": [
        {
            "symbol": "AAPL",
            "shortName": name,
            "regularMarketPrice": 101,
            "regularMarketChangePercent": 1.5,
            "regularMarketVolume": count * 100,
        }
    ]
}

try:
    occ = "AAPL270115C00100000"
    assert pt.option_price(occ) == 5
    assert pt.live_price("aapl") == 101
    assert pt.live_price(occ) == 5
    assert pt.search_assets("apple", 3)[0]["symbol"] == "AAPL"
    validated = pt.validate_asset(" aapl ")
    assert validated["valid"] and validated["price"] == 101

    for kind in ("bars", "quotes", "trades"):
        result = pt.market_history("AAPL", kind=kind, timeframe="1Day", limit=2)
        assert result["kind"] == kind and len(result["data"]) == 2
    assert (
        pt.market_history("AAPL", timeframe="1Hour", limit=1)["data"][0]["close"] == 101
    )

    quote = pt.latest_quote("AAPL")
    assert quote["bid"] == 100.5 and quote["ask"] == 101.5 and not quote["indicative"]
    assert pt.latest_trade("AAPL")["price"] == 101
    snap = pt.market_snapshot("AAPL")
    assert snap["previous_close"] == 100 and snap["change"] == 1
    news = pt.market_news("AAPL")
    assert news[0]["publisher"] == "Example Wire" and news[0]["url"].endswith("story")
    assert pt.market_screener("most-actives", 5)[0]["symbol"] == "AAPL"
    assert pt.market_screener("day_gainers", 5)[0]["change_percent"] == 1.5
    book = pt.crypto_orderbook("BTC-USD")
    assert book["indicative"] and book["bids"][0]["price"] == 100.5

    closes = pt._daily_closes(["AAPL", "MSFT", occ], "2026-07-15", "2026-07-16")
    assert closes["AAPL"]["2026-07-16"] == 101
    assert closes["MSFT"]["2026-07-15"] == 100
    assert closes[occ] == {}
    assert pt._fetch_corporate_actions("AAPL", "2026-07-15", "2026-07-17") == []
    assert pbt._default_history("AAPL", "2026-07-15", "2026-07-16").iloc[-1] == 101

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        pt.show_chain("AAPL")
        pt.show_chain("AAPL", "2027-01-15")
    assert (
        "expiries" in output.getvalue() and "AAPL270115C00100000" in output.getvalue()
    )

    conn = pt.db()
    pt.create_account(conn, "market", 10_000)
    pt.place(conn, "market", "AAPL", "buy", 1, None, price_fn=lambda _symbol: 100)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        pt.pnl(conn, "market", price_fn=lambda _symbol: 101)
        pt.show_perf(conn, "market")
    assert "equity" in output.getvalue() and "return" in output.getvalue()
    wid = pt.create_watchlist(conn, "market", "Mixed", "AAPL,BAD")
    watched = pt.watchlist_quotes(
        conn,
        "market",
        wid,
        price_fn=lambda symbol: (
            101
            if symbol == "AAPL"
            else (_ for _ in ()).throw(SystemExit("unavailable"))
        ),
    )
    assert watched["quotes"][0]["price"] == 101
    assert watched["quotes"][1]["error"] == "unavailable"
    conn.close()

    data, symbols, default = dashboard.snapshot("market")
    assert default == "market" and "AAPL" in symbols and data[0][0] == "market"
    fetched = dashboard.fetch_quotes({"AAPL", occ})
    assert fetched["AAPL"] == (101.0, 100.0) and fetched[occ] == (5.0, None)
    dashboard.run_tick(fetched)

    original_clock = pt.market_clock
    pt.market_clock = lambda: {"is_open": True}
    try:
        assert pt.market_open()
    finally:
        pt.market_clock = original_clock

    # Provider and validation failures are translated into user-facing errors.
    try:
        pt.market_history("AAPL", kind="depth")
        raise AssertionError("invalid history kind accepted")
    except SystemExit as exc:
        assert "history kind" in str(exc)
    try:
        pt.market_screener("invalid")
        raise AssertionError("invalid screener accepted")
    except SystemExit as exc:
        assert "screener" in str(exc)
    try:
        pt.option_price("AAPL270115C00105000")
        raise AssertionError("missing strike accepted")
    except SystemExit as exc:
        assert "strike" in str(exc)
finally:
    yf.Ticker, yf.Search, yf.screen = original_ticker, original_search, original_screen
    tmp.cleanup()

print("market-data checks passed")
