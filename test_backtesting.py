"""Deterministic portfolio-backtest checks with no market-data network calls."""

import os
import tempfile

import pandas as pd


os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")

import papertrade as pt
from portfolio_backtest import parse_lookback_days, run_portfolio_backtest


conn = pt.db()
pt.create_account(conn, "alpha", 9_000)
pt.create_account(conn, "beta", 4_000, make_default=False)
pt.create_account(conn, "empty", 10_000, make_default=False)
pt.create_account(conn, "short", 11_000, make_default=False)
pt.create_account(conn, "future", 8_000, make_default=False)
pt.create_account(conn, "invalid", 0, make_default=False)
pt.create_account(conn, "nodata", 5_000, make_default=False)
with pt.writing(conn):
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("alpha", "AAPL", 10, 95, 1, "spot", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("alpha", "AAPL270115C00100000", 1, 5, 100, "option", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("beta", "MSFT", 5, 200, 1, "spot", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("short", "AAPL", -10, 100, 1, "spot", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("future", "ES=F", 1, 100, 5, "future", 2_000),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("invalid", "AAPL", -10, 100, 1, "spot", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("nodata", "BAD", 1, 1, 1, "spot", 0),
    )

dates = pd.date_range("2024-01-02", periods=5, freq="B")
history = {
    "AAPL": pd.Series([100, 101, 102, 103, 104], index=dates),
    "MSFT": pd.Series([200, 199, 201, 205, 206], index=dates),
    "ES=F": pd.Series([100, 102, 104, 108, 110], index=dates),
}
requested = []


def fake_history(symbol, start, end):
    requested.append((symbol, start, end))
    return history[symbol]


result = run_portfolio_backtest(
    conn,
    "alpha",
    start="2024-01-01",
    end="2024-01-10",
    commission=0,
    history_fn=fake_history,
)
assert result["status"] == "ok", result
assert result["engine"] == "backtesting.py" and result["engine_version"]
assert [item["symbol"] for item in result["symbols"]] == ["AAPL"]
assert [item[0] for item in requested] == ["AAPL"]  # beta never leaks into alpha
assert result["bars"] == 5 and len(result["curve"]) == 5
assert abs(result["metrics"]["gross_hold_return_pct"] - 0.4) < 1e-9
assert 0.39 < result["metrics"]["return_pct"] <= 0.4
elapsed_days = (dates[-1] - dates[0]).days
expected_cagr = (
    (result["metrics"]["final_equity"] / result["metrics"]["initial_equity"])
    ** (365.2425 / elapsed_days)
    - 1
) * 100
assert abs(result["metrics"]["cagr_pct"] - expected_cagr) < 1e-9
assert result["metrics"]["max_drawdown_pct"] <= 0
assert result["metrics"]["trades"] == 1
assert result["universe_source"].endswith("selected account 'alpha'.")
assert result["skipped"][0]["symbol"] == "AAPL270115C00100000"
assert "look-ahead" in result["warnings"][0]

assert parse_lookback_days("") == 1825
assert parse_lookback_days("6m") == 183
assert parse_lookback_days("10y") == 3650
assert parse_lookback_days("max") == 36500
assert parse_lookback_days("2500d") == 2500
for invalid_period in ("3 months", "1d", "40000"):
    try:
        parse_lookback_days(invalid_period)
    except SystemExit:
        pass
    else:
        raise AssertionError(f"invalid history period accepted: {invalid_period}")

empty = run_portfolio_backtest(conn, "empty", history_fn=fake_history)
assert empty["status"] == "no_positions" and not empty["curve"]
assert len(requested) == 1  # empty portfolio performs no market-data request

short = run_portfolio_backtest(
    conn,
    "short",
    start="2024-01-01",
    end="2024-01-10",
    commission=0,
    history_fn=fake_history,
)
assert short["status"] == "ok"
assert -0.4 <= short["metrics"]["return_pct"] < -0.39

future = run_portfolio_backtest(
    conn,
    "future",
    start="2024-01-01",
    end="2024-01-10",
    commission=0,
    history_fn=fake_history,
)
assert future["status"] == "ok"
assert abs(future["metrics"]["gross_hold_return_pct"] - 0.5) < 1e-9
assert any("roll costs" in warning for warning in future["warnings"])

invalid = run_portfolio_backtest(
    conn,
    "invalid",
    start="2024-01-01",
    end="2024-01-10",
    history_fn=fake_history,
)
assert invalid["status"] == "invalid_equity" and not invalid["curve"]

nodata = run_portfolio_backtest(
    conn,
    "nodata",
    start="2024-01-01",
    end="2024-01-10",
    history_fn=fake_history,
)
assert nodata["status"] == "no_data"
assert nodata["skipped"][0]["symbol"] == "BAD"

try:
    run_portfolio_backtest(
        conn,
        "empty",
        start="2024-01-10",
        end="2024-01-10",
        history_fn=fake_history,
    )
except SystemExit as exc:
    assert "start must be before end" in str(exc)
else:
    raise AssertionError("invalid date window should fail")

try:
    run_portfolio_backtest(conn, "missing", history_fn=fake_history)
except SystemExit as exc:
    assert "no account 'missing'" in str(exc)
else:
    raise AssertionError("missing account should fail")

conn.close()
print("portfolio backtesting checks passed")
