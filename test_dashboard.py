"""Non-interactive first-run dashboard smoke check."""

import os
import sys
import tempfile
from types import SimpleNamespace


os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")

import dashboard
from rich.console import Console

assert "pandas" not in sys.modules  # backtesting stays lazy during normal dashboard use


class FakeConsole:
    def __init__(self):
        self.answers = iter(["main", "25000", "conservative"])

    def clear(self):
        pass

    def print(self, *_args, **_kwargs):
        pass

    def input(self, *_args, **_kwargs):
        return next(self.answers)


dashboard.time.sleep = lambda _seconds: None
dashboard.first_run_setup(FakeConsole())
conn = dashboard.pt.db()
assert (
    conn.execute("SELECT cash FROM accounts WHERE name='main'").fetchone()[0] == 25_000
)
assert (
    conn.execute("SELECT value FROM config WHERE key='setup_done'").fetchone()[0] == "1"
)
limits = dashboard.pt.risk_limits(conn, "main")
assert not limits["allow_short"] and limits["max_gross_leverage"] == 1
assert limits["max_order_notional"] == 6_250
assert (
    conn.execute("SELECT COUNT(*) FROM audit_log WHERE source='dashboard'").fetchone()[
        0
    ]
    == 2
)
conn.close()

recording = Console(record=True, width=150, color_system=None)
recording.print(
    dashboard.backtesting_graphs_view(
        "main",
        [("2026-01-01", 25_000), ("2026-01-02", 25_000)],
        {
            "status": "no_positions",
            "start": "2024-01-01",
            "end": "2026-01-01",
            "message": "No open positions in this portfolio to backtest.",
            "symbols": [],
            "skipped": [],
            "hypothesis": "Today's open quantities and cash were held unchanged.",
            "commission_bps": 10,
            "warnings": ["Current holdings create look-ahead bias."],
        },
    )
)
screen = recording.export_text()
assert "BACKTESTING & GRAPHS" in screen
assert "CURRENT PERFORMANCE" in screen
assert "CURRENT PORTFOLIO BACKTEST" in screen
assert "No open positions in this portfolio" in screen

recording = Console(record=True, width=150, color_system=None)
recording.print(
    dashboard.backtesting_graphs_view(
        "main",
        [("2026-01-01", 25_000), ("2026-01-02", 25_100)],
        {
            "status": "ok",
            "start": "2024-01-01",
            "end": "2026-01-01",
            "bars": 2,
            "curve": [
                {"date": "2024-01-01", "equity": 25_000},
                {"date": "2026-01-01", "equity": 27_000},
            ],
            "metrics": {
                "initial_equity": 25_000,
                "final_equity": 27_000,
                "return_pct": 8.0,
                "cagr_pct": 3.92,
                "max_drawdown_pct": -2.0,
                "sharpe": 0.8,
                "sortino": None,
                "annual_volatility_pct": 12.0,
                "commissions": None,
            },
            "symbols": [{"symbol": "AAPL"}],
            "skipped": [],
            "hypothesis": "Today's open quantities and cash were held unchanged.",
            "commission_bps": 10,
            "warnings": ["Current holdings create look-ahead bias."],
        },
    )
)
screen = recording.export_text()
assert "AAPL" in screen and "SHARPE / SORTINO" in screen and "8.00%" in screen


class GraphConsole:
    def __init__(self):
        self.answers = iter(["main", "10y", ""])

    def clear(self):
        pass

    def print(self, *_args, **_kwargs):
        pass

    def input(self, *_args, **_kwargs):
        return next(self.answers)


import portfolio_backtest as pbt  # noqa: E402  (verify lazy import before loading it)

captured = {}
original_performance = dashboard.pt.account_performance
original_cache = pbt.YahooHistoryCache
original_backtest = pbt.run_portfolio_backtest


def fake_performance(*_args, **kwargs):
    kwargs["closes_fn"](["AAPL"], "2026-01-01", "2026-01-02")
    return [("2026-01-01", 25_000), ("2026-01-02", 25_000)], None


dashboard.pt.account_performance = fake_performance
pbt.YahooHistoryCache = lambda: type(
    "HistoryCache",
    (),
    {"history": lambda *_args: {}, "daily_closes": lambda *_args, **_kwargs: {}},
)()


def fake_backtest(_conn, account, **kwargs):
    captured.update(account=account, **kwargs)
    return {
        "status": "no_positions",
        "start": "2016-01-01",
        "end": "2026-01-01",
        "message": "No open positions in this portfolio to backtest.",
        "symbols": [],
        "skipped": [],
        "hypothesis": "Current holdings retrospective.",
        "commission_bps": 10,
        "warnings": [],
    }


pbt.run_portfolio_backtest = fake_backtest
try:
    dashboard.prompt_backtesting_graphs(GraphConsole(), None)
finally:
    dashboard.pt.account_performance = original_performance
    pbt.YahooHistoryCache = original_cache
    pbt.run_portfolio_backtest = original_backtest

assert captured["account"] == "main"
assert captured["lookback_days"] == 3650

# Quote outages retain cost-basis equity, and every pending order type renders safely.
recording = Console(record=True, width=150, color_system=None)
recording.print(
    dashboard.render(
        [
            (
                "main",
                24_900,
                25_000,
                0,
                [("MSFT", 1, 100, 1, "spot", 0)],
                [
                    (1, "sell", 1, "MSFT", "stop", None, 95, None, None, "gtc"),
                    (
                        2,
                        "sell",
                        1,
                        "MSFT",
                        "trailing_stop",
                        None,
                        94,
                        None,
                        5,
                        "gtc",
                    ),
                ],
            )
        ],
        {"MSFT": None},
        "main",
    )
)
screen = recording.export_text()
assert "25,000.00" in screen and "~100.00" in screen
assert "stop 95.00" in screen and "trail 5.00% (stop 94.00)" in screen


class InvalidOptionConsole:
    def __init__(self):
        self.answers = iter(["", "", "AAPL", "not-a-date", "C", "100", "1", ""])
        self.messages = []

    def print(self, message="", *_args, **_kwargs):
        self.messages.append(str(message))

    def input(self, *_args, **_kwargs):
        return next(self.answers)


invalid_option = InvalidOptionConsole()
dashboard.prompt_option(invalid_option)
assert any("YYYY-MM-DD" in message for message in invalid_option.messages)


class ActionConsole:
    def __init__(self, answers):
        self.answers = iter(answers)

    def print(self, *_args, **_kwargs):
        pass

    def input(self, *_args, **_kwargs):
        return next(self.answers)


dashboard.prompt_new(ActionConsole(["other", "5000"]))
dashboard.prompt_order(ActionConsole(["other", "AAPL", "1", "50"]), "buy")
conn = dashboard.pt.db()
(pending_id,) = conn.execute(
    "SELECT id FROM orders WHERE account='other' AND status='pending'"
).fetchone()
conn.close()
dashboard.prompt_rename(ActionConsole(["other", "renamed"]))
dashboard.prompt_use(ActionConsole(["main"]))
dashboard.prompt_cancel(ActionConsole([str(pending_id)]))
conn = dashboard.pt.db()
assert conn.execute("SELECT 1 FROM accounts WHERE name='renamed'").fetchone()
assert (
    conn.execute("SELECT value FROM config WHERE key='default_account'").fetchone()[0]
    == "main"
)
assert (
    conn.execute("SELECT status FROM orders WHERE id=?", (pending_id,)).fetchone()[0]
    == "canceled"
)
conn.close()

# The non-TTY read path and the full dashboard loop both return cleanly.
assert dashboard.read_key(0) is None


class DummyLive:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def update(self, *_args, **_kwargs):
        pass


saved = {
    "Live": dashboard.Live,
    "snapshot": dashboard.snapshot,
    "fetch_quotes": dashboard.fetch_quotes,
    "render": dashboard.render,
    "read_key": dashboard.read_key,
    "run_dashboard": dashboard.run_dashboard,
}
try:
    dashboard.Live = lambda **_kwargs: DummyLive()
    dashboard.snapshot = lambda _account=None: ([], set(), None)
    dashboard.fetch_quotes = lambda _symbols, executor=None: {}
    dashboard.render = lambda *_args, **_kwargs: "dashboard"
    dashboard.read_key = lambda _timeout: "q"
    assert (
        dashboard.run_dashboard(
            ActionConsole([]), SimpleNamespace(account=None, interval=0.01)
        )
        == "quit"
    )
    dashboard.run_dashboard = lambda _console, _args: "quit"
    dashboard.main()
finally:
    for name, value in saved.items():
        setattr(dashboard, name, value)

print("dashboard checks passed")
