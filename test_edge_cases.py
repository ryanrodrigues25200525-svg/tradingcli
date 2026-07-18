"""Regression checks for failure paths and linked-order lifecycle invariants."""

import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone


tmp = tempfile.TemporaryDirectory()
os.environ["PAPERTRADE_DB"] = str(Path(tmp.name) / "edge-cases.db")

import papertrade as pt  # noqa: E402


def rejected(call, message):
    try:
        call()
    except SystemExit as exc:
        assert message in str(exc), str(exc)
    else:
        raise AssertionError(f"expected SystemExit containing {message!r}")


conn = pt.db()
pt.create_account(conn, "main", 100_000)

# Opening/closing auction orders are accepted only inside their bounded windows.
monday_open = datetime(2026, 7, 20, 13, 35, tzinfo=timezone.utc)  # 09:35 ET
monday_close = datetime(2026, 7, 20, 19, 55, tzinfo=timezone.utc)  # 15:55 ET
assert pt._auction_ready("opg", monday_open)
assert pt._auction_ready("cls", monday_close)
assert not pt._auction_ready("opg", monday_close)

# Crossing zero must realize the closed side and open the remainder at the new fill.
pt.fill(conn, "main", "AAPL", "buy", 10, 100)
pt.fill(conn, "main", "AAPL", "sell", 15, 110)
qty, avg = conn.execute(
    "SELECT qty,avg_cost FROM positions WHERE account='main' AND symbol='AAPL'"
).fetchone()
assert (qty, avg) == (-5, 110), (qty, avg)
pt.fill(conn, "main", "AAPL", "buy", 5, 90)
realized = conn.execute("SELECT realized FROM accounts WHERE name='main'").fetchone()[0]
assert realized == 200, realized

# Options always use whole contracts, including the legacy entry and direct-fill paths.
occ = "AAPL270115C00100000"
rejected(lambda: pt.fill(conn, "main", occ, "buy", 0.5, 2), "whole number")
rejected(
    lambda: pt.place(conn, "main", occ, "buy", 0.5, None, price_fn=lambda _symbol: 2),
    "whole number",
)
rejected(
    lambda: pt.submit_option_multileg(
        conn,
        "main",
        [
            {"symbol": occ, "side": "buy", "qty": None},
            {"symbol": "AAPL270115C00110000", "side": "sell", "qty": 1},
        ],
        price_fn=lambda _symbol: 2,
    ),
    "leg quantity",
)
rejected(
    lambda: pt.submit_order(
        conn,
        "main",
        "MSFT",
        "buy",
        qty=1,
        order_type="limit",
        limit_price=90,
        time_in_force="ioc",
        order_class="bracket",
        take_profit=120,
        stop_loss=80,
        price_fn=lambda _symbol: 100,
    ),
    "not supported for linked orders",
)

# Canceling a bracket parent cancels held children instead of orphaning them.
bracket = pt.submit_order(
    conn,
    "main",
    "MSFT",
    "buy",
    qty=1,
    order_type="limit",
    limit_price=90,
    order_class="bracket",
    take_profit=120,
    stop_loss=80,
)
pt.cancel(conn, bracket)
statuses = {
    status
    for (status,) in conn.execute(
        "SELECT status FROM orders WHERE id=? OR parent_id=?", (bracket, bracket)
    )
}
assert statuses == {"canceled"}, statuses

# Expiring a linked parent also retires held exits.
expiring = pt.submit_order(
    conn,
    "main",
    "NVDA",
    "buy",
    qty=1,
    order_type="limit",
    limit_price=90,
    time_in_force="day",
    order_class="bracket",
    take_profit=120,
    stop_loss=80,
)
conn.execute("UPDATE orders SET ts='2000-01-01T00:00:00+00:00' WHERE id=?", (expiring,))
conn.commit()
pt.tick(conn, price_fn=lambda _symbol: 100)
states = dict(
    conn.execute(
        "SELECT id,status FROM orders WHERE id=? OR parent_id=?", (expiring, expiring)
    )
)
assert states[expiring] == "expired"
assert set(states.values()) == {"expired", "canceled"}, states

# If a pending parent becomes unfillable, rejection also retires held exits.
pt.create_account(conn, "reject", 100, make_default=False)
rejected_parent = pt.submit_order(
    conn,
    "reject",
    "AMD",
    "buy",
    qty=1,
    order_type="limit",
    limit_price=90,
    order_class="bracket",
    take_profit=120,
    stop_loss=80,
)
pt.adjust_cash(conn, "reject", -100)
pt.tick(conn, price_fn=lambda _symbol: 80)
rejected_states = dict(
    conn.execute(
        "SELECT id,status FROM orders WHERE id=? OR parent_id=?",
        (rejected_parent, rejected_parent),
    )
)
assert rejected_states[rejected_parent] == "rejected"
assert set(rejected_states.values()) == {"rejected", "canceled"}, rejected_states

# Either OCO leg is a group cancel: no sibling may remain live.
pt.fill(conn, "main", "TSLA", "buy", 2, 100)
oco = pt.submit_order(
    conn,
    "main",
    "TSLA",
    "sell",
    qty=2,
    order_class="oco",
    take_profit=120,
    stop_loss=80,
    price_fn=lambda _symbol: 100,
)
(oco_child,) = conn.execute(
    "SELECT id FROM orders WHERE parent_id=?", (oco,)
).fetchone()
pt.cancel(conn, oco_child)
assert {
    status
    for (status,) in conn.execute(
        "SELECT status FROM orders WHERE id=? OR parent_id=?", (oco, oco)
    )
} == {"canceled"}

# A manual close supersedes active same-symbol exits so they cannot reopen a short.
pending_exit = pt.place(
    conn, "main", "TSLA", "sell", 2, 200, price_fn=lambda _symbol: 100
)
pt.close_position(conn, "main", "TSLA", price_fn=lambda _symbol: 100)
assert (
    conn.execute("SELECT status FROM orders WHERE id=?", (pending_exit,)).fetchone()[0]
    == "canceled"
)
assert not conn.execute(
    "SELECT 1 FROM positions WHERE account='main' AND symbol='TSLA'"
).fetchone()

# Deleting the current default elects a deterministic replacement.
pt.create_account(conn, "backup", 1_000, make_default=False)
pt.set_default(conn, "main")
pt.wipe_account(conn, "main")
assert (
    conn.execute("SELECT value FROM config WHERE key='default_account'").fetchone()[0]
    == "backup"
)

# Rapid backups must never overwrite one another and each copy must be consistent.
backup_dir = Path(tmp.name) / "backups"
expected_accounts = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
first = pt.backup_database(conn, str(backup_dir))
second = pt.backup_database(conn, str(backup_dir))
assert first != second and Path(first).exists() and Path(second).exists()
for path in (first, second):
    copy = sqlite3.connect(path)
    try:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert (
            copy.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
            == expected_accounts
        )
    finally:
        copy.close()

conn.close()
tmp.cleanup()
print("edge-case checks passed")
