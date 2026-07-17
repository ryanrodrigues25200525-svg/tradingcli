"""Deterministic randomized accounting invariants across supported asset models."""

import random
import time

import papertrade as pt


rng = random.Random(20260718)
cases = (
    ("AAPL", lambda: rng.uniform(20, 300)),
    ("AAPL270115C00100000", lambda: rng.uniform(0.25, 40)),
    ("MES=F", lambda: rng.uniform(3_000, 7_000)),
)
operations = 0
started = time.perf_counter()

for symbol, next_price in cases:
    state = {"cash": 1_000_000_000.0, "realized": 0.0, "pos": {}}
    contributed = state["cash"]
    for _ in range(5_000):
        side = rng.choice(("buy", "sell"))
        qty = rng.randint(1, 20)
        price = next_price()
        pt._apply(state, symbol, side, qty, price)
        position = state["pos"].get(symbol)
        mark = next_price()
        if position:
            unrealized = position["qty"] * position["mult"] * (mark - position["avg"])
            liquidation = (
                unrealized + position["margin"]
                if position["ac"] == "future"
                else position["qty"] * position["mult"] * mark
            )
        else:
            unrealized = liquidation = 0.0
        equity = state["cash"] + liquidation
        expected = contributed + state["realized"] + unrealized
        assert abs(equity - expected) < 1e-4, (
            symbol,
            side,
            qty,
            price,
            equity,
            expected,
            position,
        )
        operations += 1

elapsed = time.perf_counter() - started
assert elapsed < 5, elapsed
print(f"accounting invariants passed ({operations:,} fills in {elapsed:.3f}s)")
