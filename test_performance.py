"""Performance guardrails for parallel pricing, history reuse, and metrics."""

import math
import os
import tempfile
import threading
import time

import pandas as pd


os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")

import papertrade as pt
from portfolio_backtest import YahooHistoryCache


conn = pt.db()
pt.create_account(conn, "speed", 100_000)
with pt.writing(conn):
    for index in range(8):
        conn.execute(
            "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin) "
            "VALUES(?,?,?,?,?,?,?)",
            ("speed", f"S{index}", 1, 100, 1, "spot", 0),
        )

lock = threading.Lock()
active = max_active = 0


def slow_price(_symbol):
    global active, max_active
    with lock:
        active += 1
        max_active = max(max_active, active)
    time.sleep(0.02)
    with lock:
        active -= 1
    return 100.0


started = time.perf_counter()
equity = pt.current_equity(conn, "speed", price_fn=slow_price)
pricing_seconds = time.perf_counter() - started
assert equity == 100_800
assert max_active > 1  # prevents regression to serial network pricing

dates = pd.date_range("2024-01-02", periods=8, freq="B")
history_calls = []
history_active = history_max_active = 0


def slow_history(symbol, start, end):
    global history_active, history_max_active
    with lock:
        history_active += 1
        history_max_active = max(history_max_active, history_active)
        history_calls.append((symbol, start, end))
    time.sleep(0.02)
    with lock:
        history_active -= 1
    return pd.Series(range(100, 108), index=dates, dtype=float)


cache = YahooHistoryCache(loader=slow_history)
closes = cache.daily_closes(
    ["AAPL", "MSFT", "NVDA", "BTC-USD"], "2024-01-01", "2024-01-31"
)
assert set(closes) == {"AAPL", "MSFT", "NVDA", "BTC-USD"}
assert history_max_active > 1  # historical symbols load concurrently
assert len(history_calls) == 4
cache.history("AAPL", "2024-01-03", "2024-01-10")
assert len(history_calls) == 4  # graph history is reused by the backtest

# A 100% deposit must not appear as investment return. The underlying returns
# 10% on both days, making the time-weighted result 21%, not the raw 121%.
flow_curve = [
    ("2026-01-01", 1_000),
    ("2026-01-02", 1_100),
    ("2026-01-03", 2_210),
]
metrics = pt.performance_metrics(
    flow_curve, cashflows=[("2026-01-03T12:00:00+00:00", 1_000)]
)
assert abs(metrics["total"] - 0.21) < 1e-12

returns = [0.01, -0.005, 0.002]
equity_values = [1_000]
for period_return in returns:
    equity_values.append(equity_values[-1] * (1 + period_return))
calendar_curve = [
    (f"2026-01-0{index + 1}", value) for index, value in enumerate(equity_values)
]
calendar_metrics = pt.performance_metrics(calendar_curve)
mean = sum(returns) / len(returns)
variance = sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)
expected_volatility = math.sqrt(variance) * math.sqrt(365)
assert abs(calendar_metrics["vol"] - expected_volatility) < 1e-12

# A second CLI process should reuse a recent price instead of repeating a
# network request. Clearing the memory cache simulates a fresh process.
provider_calls = 0


def counted_price():
    global provider_calls
    provider_calls += 1
    return 123.45


pt.features._price_cache.clear()
assert pt.features.provider_price(
    "CACHE-SPEED", counted_price, ttl=30, database=os.environ["PAPERTRADE_DB"]
) == 123.45
pt.features._price_cache.clear()
assert pt.features.provider_price(
    "CACHE-SPEED",
    lambda: (_ for _ in ()).throw(AssertionError("cache miss")),
    ttl=30,
    database=os.environ["PAPERTRADE_DB"],
) == 123.45
assert provider_calls == 1

conn.close()
print(
    "performance checks passed "
    f"(parallel pricing {pricing_seconds * 1000:.1f}ms, "
    f"workers {max_active}, cached history requests {len(history_calls)})"
)
