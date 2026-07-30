"""Regression coverage for the Alpaca-style schema feature set."""

import importlib
import json
import os
import tempfile


os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")

import papertrade as pt


prices = {
    "AAPL": 100.0,
    "MSFT": 200.0,
    "AAPL270115C00100000": 5.0,
    "AAPL270115C00110000": 2.0,
}


def fake(symbol):
    return prices[symbol]


def no_price(_symbol):
    raise AssertionError("idempotent replay unexpectedly fetched a market price")


conn = pt.db()
assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
assert {
    "order_type",
    "stop_price",
    "trail_price",
    "time_in_force",
    "client_order_id",
    "parent_id",
    "order_class",
} <= pt._cols(conn, "orders")
assert {"watchlists", "watchlist_symbols", "option_instructions"} <= {
    name
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
}
pt.create_account(conn, "parity", 200_000)
pt.create_account(conn, "market-retry", 10_000, make_default=False)

market_id = pt.submit_order(
    conn,
    "market-retry",
    "AAPL",
    "buy",
    qty=1,
    order_type="market",
    price_fn=fake,
    source="codex",
    request_id="market-retry",
)
assert (
    pt.submit_order(
        conn,
        "market-retry",
        "AAPL",
        "buy",
        qty=1,
        order_type="market",
        price_fn=no_price,
        source="codex",
        request_id="market-retry",
    )
    == market_id
)

# Pending opening orders reserve buying power like a broker order book.
pt.create_account(conn, "reserved", 1_000, make_default=False)
pt.submit_order(
    conn, "reserved", "AAPL", "buy", qty=8, order_type="limit", limit_price=100
)
try:
    pt.submit_order(
        conn, "reserved", "AAPL", "buy", qty=3, order_type="limit", limit_price=100
    )
    raise AssertionError("open orders did not reserve buying power")
except SystemExit as exc:
    assert "open orders" in str(exc)

# Generic order lifecycle: client id, replacement, lookup, fill, and bulk cancel.
oid = pt.submit_order(
    conn,
    "parity",
    "AAPL",
    "buy",
    qty=2,
    order_type="limit",
    limit_price=90,
    client_order_id="replace-me",
    source="codex",
    request_id="submit-1",
)
assert pt.get_order(conn, client_order_id="replace-me", account="parity")["id"] == oid
new_id = pt.replace_order(
    conn,
    oid,
    limit_price=95,
    client_order_id="replacement",
    source="codex",
    request_id="replace-1",
)
assert pt.get_order(conn, order_id=oid)["status"] == "replaced"
prices["AAPL"] = 94
pt.tick(conn, price_fn=fake)
assert pt.get_order(conn, order_id=new_id)["status"] == "filled"
assert pt.get_position(conn, "parity", "AAPL", fake)["signed_qty"] == 2

# Stop and stop-limit orders trigger in the correct direction.
stop_id = pt.submit_order(
    conn,
    "parity",
    "AAPL",
    "buy",
    qty=1,
    order_type="stop",
    stop_price=105,
    price_fn=fake,
)
prices["AAPL"] = 104
pt.tick(conn, price_fn=fake)
assert pt.get_order(conn, order_id=stop_id)["status"] == "pending"
prices["AAPL"] = 106
pt.tick(conn, price_fn=fake)
assert pt.get_order(conn, order_id=stop_id)["status"] == "filled"

stop_limit_id = pt.submit_order(
    conn,
    "parity",
    "AAPL",
    "sell",
    qty=1,
    order_type="stop-limit",
    stop_price=100,
    limit_price=99,
    price_fn=fake,
)
prices["AAPL"] = 98
pt.tick(conn, price_fn=fake)
stop_limit = pt.get_order(conn, order_id=stop_limit_id)
assert stop_limit["triggered"] and stop_limit["status"] == "pending"
prices["AAPL"] = 99.5
pt.tick(conn, price_fn=fake)
assert pt.get_order(conn, order_id=stop_limit_id)["status"] == "filled"

# Bracket exits activate after entry and cancel their sibling after one exit fills.
prices["AAPL"] = 100
bracket = pt.submit_order(
    conn,
    "parity",
    "AAPL",
    "buy",
    qty=1,
    order_type="market",
    order_class="bracket",
    take_profit={"limit_price": 110},
    stop_loss={"stop_price": 90},
    price_fn=fake,
)
children = pt.get_order(conn, order_id=bracket)["children"]
assert len(children) == 2 and {child["status"] for child in children} == {"pending"}
prices["AAPL"] = 111
pt.tick(conn, price_fn=fake)
states = {
    row[0]
    for row in conn.execute("SELECT status FROM orders WHERE parent_id=?", (bracket,))
}
assert states == {"filled", "canceled"}

# Trailing stops update their high-water mark and then close on reversal.
pt.submit_order(
    conn, "parity", "MSFT", "buy", qty=1, order_type="market", price_fn=fake
)
trail = pt.submit_order(
    conn,
    "parity",
    "MSFT",
    "sell",
    qty=1,
    order_type="trailing-stop",
    trail_price=5,
    price_fn=fake,
)
prices["MSFT"] = 220
pt.tick(conn, price_fn=fake)
assert pt.get_order(conn, order_id=trail)["hwm"] == 220
prices["MSFT"] = 214
pt.tick(conn, price_fn=fake)
assert pt.get_order(conn, order_id=trail)["status"] == "filled"

# Partial close, full close-all, and cancel-all.
prices["AAPL"] = 100
pt.create_account(conn, "close-retry", 10_000, make_default=False)
pt.place(conn, "close-retry", "AAPL", "buy", 1, None, price_fn=fake)
closed_id = pt.close_position(
    conn,
    "close-retry",
    "AAPL",
    price_fn=fake,
    source="codex",
    request_id="close-once",
)
assert (
    pt.close_position(
        conn,
        "close-retry",
        "AAPL",
        price_fn=no_price,
        source="codex",
        request_id="close-once",
    )
    == closed_id
)
before = pt.get_position(conn, "parity", "AAPL", fake)["signed_qty"]
pt.close_position(conn, "parity", "AAPL", percent=50, price_fn=fake)
after = pt.get_position(conn, "parity", "AAPL", fake)["signed_qty"]
assert abs(after - before / 2) < 1e-9
pt.submit_order(
    conn, "parity", "MSFT", "buy", qty=1, order_type="limit", limit_price=150
)
assert pt.cancel_all_orders(conn, "parity") == 1
pt.close_all_positions(conn, "parity", price_fn=fake)
assert pt.list_positions(conn, "parity", fake) == []

# Persistent watchlist CRUD and unified activity feed.
wid = pt.create_watchlist(conn, "parity", "Tech", "AAPL,MSFT")
pt.add_watchlist_symbol(conn, "parity", wid, "NVDA")
assert pt.get_watchlist(conn, "parity", wid)["symbols"] == ["AAPL", "MSFT", "NVDA"]
pt.remove_watchlist_symbol(conn, "parity", wid, "NVDA")
assert len(pt.list_watchlists(conn, "parity")) == 1
assert any(event["type"] == "fill" for event in pt.account_activities(conn, "parity"))
temporary = pt.create_watchlist(conn, "parity", "Temporary")
pt.delete_watchlist(
    conn, "parity", temporary, source="codex", request_id="delete-watchlist"
)
pt.delete_watchlist(
    conn, "parity", temporary, source="codex", request_id="delete-watchlist"
)

# Option exercise creates underlying shares; DNE suppresses intrinsic settlement.
pt.create_account(conn, "exercise", 100_000, make_default=False)
pt.place(
    conn,
    "exercise",
    "AAPL270115C00100000",
    "buy",
    1,
    None,
    price_fn=fake,
)
pt.exercise_option(conn, "exercise", "AAPL270115C00100000")
assert (
    conn.execute(
        "SELECT qty FROM positions WHERE account='exercise' AND symbol='AAPL'"
    ).fetchone()[0]
    == 100
)
assert (
    pt.option_contract_details("AAPL270115C00100000", price_fn=fake)["strike_price"]
    == 100
)

pt.create_account(conn, "exercise-retry", 100_000, make_default=False)
pt.place(
    conn,
    "exercise-retry",
    "AAPL270115C00100000",
    "buy",
    1,
    None,
    price_fn=fake,
)
pt.exercise_option(
    conn,
    "exercise-retry",
    "AAPL270115C00100000",
    source="codex",
    request_id="exercise-once",
)
pt.exercise_option(
    conn,
    "exercise-retry",
    "AAPL270115C00100000",
    source="codex",
    request_id="exercise-once",
)

pt.create_account(conn, "dne", 10_000, make_default=False)
expired = "AAPL200101C00100000"
prices[expired] = 5
pt.place(conn, "dne", expired, "buy", 1, None, price_fn=fake)
pt.do_not_exercise_option(conn, "dne", expired)
prices["AAPL"] = 200
pt.settle_expired(conn, price_fn=fake)
assert (
    conn.execute("SELECT COUNT(*) FROM positions WHERE account='dne'").fetchone()[0]
    == 0
)

# Atomic multi-leg option package.
pt.create_account(conn, "spread", 100_000, make_default=False)
ids = pt.submit_option_multileg(
    conn,
    "spread",
    [
        {"symbol": "AAPL270115C00100000", "side": "buy", "qty": 1},
        {"symbol": "AAPL270115C00110000", "side": "sell", "qty": 1},
    ],
    price_fn=fake,
)
assert len(ids) == 2
assert (
    conn.execute(
        "SELECT COUNT(*) FROM orders WHERE account='spread' AND order_class='mleg'"
    ).fetchone()[0]
    == 2
)
retry_legs = [
    {"symbol": "AAPL270115C00100000", "side": "buy", "qty": 1},
    {"symbol": "AAPL270115C00110000", "side": "sell", "qty": 1},
]
assert pt.submit_option_multileg(
    conn,
    "spread",
    retry_legs,
    price_fn=fake,
    source="codex",
    request_id="spread-retry",
)
assert (
    pt.submit_option_multileg(
        conn,
        "spread",
        retry_legs,
        price_fn=no_price,
        source="codex",
        request_id="spread-retry",
    )
    == []
)

# Calendar, schema discovery, and structured CLI output need no live market calls.
calendar = pt.market_calendar("2026-07-01", "2026-07-06")
assert calendar and all("early_close" in session for session in calendar)
buffer = pt.io.StringIO()
with pt.contextlib.redirect_stdout(buffer):
    pt.main(["--schema"])
assert "order" in json.loads(buffer.getvalue())
assert pt.healthcheck(conn)["schema_version"] == 6
conn.close()

# v2 -> v5 migration preserves accounts and fills new columns/tables.
legacy_path = tempfile.mktemp(suffix=".db")
legacy = pt.sqlite3.connect(legacy_path)
legacy.executescript(
    """
    CREATE TABLE config(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE accounts(name TEXT PRIMARY KEY, cash REAL NOT NULL,
      deposits REAL DEFAULT 0, realized REAL DEFAULT 0, created TEXT);
    CREATE TABLE positions(account TEXT, symbol TEXT, qty REAL NOT NULL,
      avg_cost REAL NOT NULL, mult REAL DEFAULT 1, asset_class TEXT DEFAULT 'spot',
      margin REAL DEFAULT 0, PRIMARY KEY(account, symbol));
    CREATE TABLE orders(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT,
      symbol TEXT, side TEXT, qty REAL, limit_price REAL, status TEXT DEFAULT 'pending',
      filled_price REAL, ts TEXT, source TEXT DEFAULT 'unknown', request_id TEXT,
      reject_reason TEXT);
    CREATE TABLE cashflow(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT,
      ts TEXT, amount REAL);
    INSERT INTO accounts(name,cash,deposits) VALUES('legacy',1234,1234);
    PRAGMA user_version=2;
    """
)
legacy.commit()
legacy.close()
os.environ["PAPERTRADE_DB"] = legacy_path
importlib.reload(pt)
migrated = pt.db()
assert migrated.execute("PRAGMA user_version").fetchone()[0] == 6
assert (
    migrated.execute("SELECT cash FROM accounts WHERE name='legacy'").fetchone()[0]
    == 1234
)
assert "order_type" in pt._cols(migrated, "orders")
migrated.close()

print("Alpaca parity checks passed")
