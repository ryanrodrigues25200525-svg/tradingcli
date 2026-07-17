"""Non-interactive first-run dashboard smoke check."""

import os
import sys
import tempfile


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

print("dashboard checks passed")
