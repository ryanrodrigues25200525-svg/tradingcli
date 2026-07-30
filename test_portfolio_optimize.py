"""Deterministic rebalance-suggestion checks with no market-data network calls."""

import os
import tempfile

import numpy as np
import pandas as pd


os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")

import papertrade as pt
from portfolio_optimize import METHODS, suggest_rebalance


conn = pt.db()
pt.create_account(conn, "alpha", 9_000)
pt.create_account(conn, "single", 5_000, make_default=False)
pt.create_account(conn, "empty", 10_000, make_default=False)
with pt.writing(conn):
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("alpha", "AAPL", 10, 95, 1, "spot", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("alpha", "MSFT", 5, 200, 1, "spot", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("alpha", "AAPL270115C00100000", 1, 5, 100, "option", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("single", "AAPL", 10, 95, 1, "spot", 0),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("alpha", "ES=F", 1, 5_000, 50, "future", 12_000),
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
        "VALUES(?,?,?,?,?,?,?)",
        ("alpha", "TSLA", -2, 250, 1, "spot", 0),
    )

dates = pd.bdate_range(end=pd.Timestamp.today().normalize() - pd.Timedelta(days=3), periods=60)
rng = np.random.default_rng(0)
history = {
    "AAPL": pd.Series(100 * (1 + rng.normal(0.0005, 0.01, 60)).cumprod(), index=dates),
    "MSFT": pd.Series(200 * (1 + rng.normal(0.0002, 0.02, 60)).cumprod(), index=dates),
}


def fake_history(symbol, start, end):
    return history[symbol]


for method in METHODS:
    result = suggest_rebalance(conn, "alpha", method=method, history_fn=fake_history)
    assert result["status"] == "ok_partial", (method, result)
    assert set(result["target_weights"]) == {"AAPL", "MSFT", "CASH"}
    total = sum(result["target_weights"].values())
    assert abs(total - 1.0) < 1e-4, (method, total)
    assert all(w >= -1e-9 for w in result["target_weights"].values())  # long-only
    assert {item["symbol"] for item in result["skipped"]} == {
        "AAPL270115C00100000",
        "ES=F",
        "TSLA",
    }
    assert set(result["drift"]) == set(result["target_weights"])
    assert result["target_weights"]["CASH"] == result["current_weights"]["CASH"]

equal = suggest_rebalance(conn, "alpha", method="equal_weight", history_fn=fake_history)
invested_target = (
    equal["target_weights"]["AAPL"] + equal["target_weights"]["MSFT"]
)
assert abs(equal["target_weights"]["AAPL"] - invested_target / 2) < 1e-6
assert abs(equal["target_weights"]["MSFT"] - invested_target / 2) < 1e-6
assert equal["current_weights"]["CASH"] > 0.70

single = suggest_rebalance(conn, "single", history_fn=fake_history)
assert single["status"] == "no_data"

empty = suggest_rebalance(conn, "empty", history_fn=fake_history)
assert empty["status"] == "no_positions" and not empty["target_weights"]

try:
    suggest_rebalance(conn, "alpha", method="bogus", history_fn=fake_history)
except SystemExit as exc:
    assert "method must be one of" in str(exc)
else:
    raise AssertionError("invalid method should fail")

try:
    suggest_rebalance(conn, "alpha", lookback_days=1, history_fn=fake_history)
except SystemExit as exc:
    assert "lookback_days must be between" in str(exc)
else:
    raise AssertionError("invalid lookback_days should fail")

conn.close()
print("portfolio optimize checks passed")
