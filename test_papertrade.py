"""Self-check with stubbed prices — no network. Run: python3 test_papertrade.py"""

import importlib
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")
import papertrade as pt

prices = {"AAPL": 100.0}


def fake(symbol):
    return prices[symbol]


def acct(conn, name="t"):
    return conn.execute(
        "SELECT cash, deposits, realized FROM accounts WHERE name=?", (name,)
    ).fetchone()


def equity(conn, name, price_fn):
    cash, deposits, realized = acct(conn, name)
    eq, unreal = cash, 0.0
    for sym, qty, avg, mult, ac, margin in conn.execute(
        "SELECT symbol,qty,avg_cost,mult,asset_class,margin FROM positions WHERE account=?",
        (name,),
    ):
        u = qty * mult * (price_fn(sym) - avg)
        unreal += u
        eq += (u + margin) if ac == "future" else qty * mult * price_fn(sym)
    # invariant: equity == deposits + realized + unrealized
    assert abs(eq - (deposits + realized + unreal)) < 1e-6, (
        eq,
        deposits,
        realized,
        unreal,
    )
    return eq


conn = pt.db()
with conn:
    conn.execute("INSERT INTO accounts(name,cash,deposits) VALUES('t',10000,10000)")
    # --- spot round trip ---
    pt.place(conn, "t", "AAPL", "buy", 50, None, price_fn=fake)
    assert acct(conn)[0] == 5000
    assert abs(equity(conn, "t", fake) - 10000) < 1e-9  # no move yet
    pt.place(conn, "t", "AAPL", "buy", 10, 95.0, price_fn=fake)
    pt.tick(conn, price_fn=fake)
    assert (
        conn.execute("SELECT status FROM orders WHERE limit_price=95").fetchone()[0]
        == "pending"
    )
    prices["AAPL"] = 94.0
    pt.tick(conn, price_fn=fake)
    assert (
        conn.execute("SELECT status FROM orders WHERE limit_price=95").fetchone()[0]
        == "filled"
    )
    qty, avg = conn.execute(
        "SELECT qty, avg_cost FROM positions WHERE symbol='AAPL'"
    ).fetchone()
    assert qty == 60 and abs(avg - 99.0) < 1e-9
    prices["AAPL"] = 110.0
    pt.place(conn, "t", "AAPL", "sell", 60, None, price_fn=fake)
    assert abs(acct(conn)[0] - 10660) < 1e-9
    assert abs(acct(conn)[2] - 660) < 1e-9  # realized = 60*(110-99)
    try:
        pt.place(conn, "t", "AAPL", "buy", 1000, None, price_fn=fake)
        raise AssertionError("overspend not blocked")
    except SystemExit:
        pass
    assert (
        conn.execute("SELECT COUNT(*) FROM orders WHERE account='t'").fetchone()[0] == 3
    )

# --- option long: spot model with mult 100 ---
os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")
importlib.reload(pt)
OCC = "AAPL260116C00150000"
prices = {"AAPL": 155.0, OCC: 5.0}
conn = pt.db()
with conn:
    conn.execute("INSERT INTO accounts(name,cash,deposits) VALUES('o',10000,10000)")
    pt.place(conn, "o", OCC, "buy", 2, None, price_fn=fake)  # pay 2*100*5 = 1000
    assert abs(acct(conn, "o")[0] - 9000) < 1e-9
    prices[OCC] = 8.0
    assert abs(equity(conn, "o", fake) - 10600) < 1e-9  # +2*100*(8-5)=600
    prices[OCC] = 8.0
    pt.place(conn, "o", OCC, "sell", 2, None, price_fn=fake)  # close
    assert abs(acct(conn, "o")[2] - 600) < 1e-9  # realized 600

# --- option expiry settlement (ITM call -> intrinsic) ---
past = "AAPL200101C00150000"  # expired 2020
prices2 = {"AAPL": 175.0, past: 20.0}
with conn:
    conn.execute("INSERT INTO accounts(name,cash,deposits) VALUES('e',10000,10000)")
    pt.place(conn, "e", past, "buy", 1, None, price_fn=lambda s: prices2[s])  # pay 2000
    pt.settle_expired(
        conn, price_fn=lambda s: prices2[s]
    )  # spot 175, strike 150 -> intrinsic 25
    assert (
        conn.execute("SELECT COUNT(*) FROM positions WHERE account='e'").fetchone()[0]
        == 0
    )
    assert abs(acct(conn, "e")[2] - 500) < 1e-9  # realized 1*100*(25-20)=500

# --- future: margin accounting round trip ---
os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")
importlib.reload(pt)
prices = {"MES=F": 5000.0}  # mult 5, margin 1200
conn = pt.db()
with conn:
    conn.execute("INSERT INTO accounts(name,cash,deposits) VALUES('f',10000,10000)")
    pt.place(conn, "f", "MES=F", "buy", 2, None, price_fn=fake)  # post 2*1200 margin
    assert abs(acct(conn, "f")[0] - 7600) < 1e-9  # 10000 - 2400
    assert abs(equity(conn, "f", fake) - 10000) < 1e-9  # no move
    prices["MES=F"] = 5010.0
    assert abs(equity(conn, "f", fake) - 10100) < 1e-9  # +2*5*10 = 100
    pt.place(conn, "f", "MES=F", "sell", 2, None, price_fn=fake)  # close
    assert abs(acct(conn, "f")[0] - 10100) < 1e-9  # margin back + P&L
    assert abs(acct(conn, "f")[2] - 100) < 1e-9  # realized 100
    assert (
        conn.execute("SELECT COUNT(*) FROM positions WHERE account='f'").fetchone()[0]
        == 0
    )

# --- cancel, deposit/withdraw, reset, delete ---
os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")
importlib.reload(pt)
prices = {"AAPL": 100.0}
conn = pt.db()
with conn:
    conn.execute("INSERT INTO accounts(name,cash,deposits) VALUES('t',10000,10000)")
    pt.place(conn, "t", "AAPL", "buy", 1, 50.0, price_fn=fake)
    (oid,) = conn.execute("SELECT id FROM orders WHERE status='pending'").fetchone()
    pt.cancel(conn, oid)
    assert (
        conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()[0]
        == "canceled"
    )
    c0 = acct(conn)[0]
    pt.adjust_cash(conn, "t", 5000)
    pt.adjust_cash(conn, "t", -1000)
    assert abs(acct(conn)[0] - (c0 + 4000)) < 1e-9
    assert abs(acct(conn)[1] - 14000) < 1e-9  # deposits tracked
    try:
        pt.adjust_cash(conn, "t", -1e9)
        raise AssertionError("overdraft allowed")
    except SystemExit:
        pass
    pt.wipe_account(conn, "t", reset_cash=100_000)
    assert acct(conn) == (100_000, 100_000, 0)
    pt.wipe_account(conn, "t")
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0

# --- rename cascades across all tables + default pointer ---
os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")
importlib.reload(pt)
prices = {"AAPL": 100.0}
conn = pt.db()
with conn:
    conn.execute(
        "INSERT INTO accounts(name,cash,deposits,created) VALUES('old',10000,10000,'2026-01-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO cashflow(account,ts,amount) VALUES('old','2026-01-02T00:00:00+00:00',10000)"
    )
    conn.execute("INSERT OR REPLACE INTO config VALUES('default_account','old')")
    pt.place(conn, "old", "AAPL", "buy", 5, None, price_fn=fake)
    pt.rename_account(conn, "old", "fresh")
    assert conn.execute("SELECT 1 FROM accounts WHERE name='fresh'").fetchone()
    assert (
        conn.execute("SELECT COUNT(*) FROM positions WHERE account='fresh'").fetchone()[
            0
        ]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM orders WHERE account='fresh'").fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM cashflow WHERE account='fresh'").fetchone()[
            0
        ]
        == 1
    )
    assert (
        conn.execute("SELECT value FROM config WHERE key='default_account'").fetchone()[
            0
        ]
        == "fresh"
    )

# --- equity curve reconstruction with stubbed daily closes ---
os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")
importlib.reload(pt)
conn = pt.db()
with conn:
    conn.execute(
        "INSERT INTO accounts(name,cash,deposits,created) VALUES('c',10000,10000,'2026-01-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO cashflow(account,ts,amount) VALUES('c','2026-01-02T00:00:00+00:00',10000)"
    )
    # buy 10 AAPL @100 on Jan 2
    conn.execute(
        "INSERT INTO orders(account,symbol,side,qty,limit_price,status,filled_price,ts)"
        " VALUES('c','AAPL','buy',10,NULL,'filled',100.0,'2026-01-02T15:00:00+00:00')"
    )

    def stub_closes(symbols, start, end):
        return {"AAPL": {"2026-01-02": 100.0, "2026-01-03": 110.0, "2026-01-04": 90.0}}

    # monkeypatch end date via closes only; curve runs start..today, so just check known days
    curve = pt.equity_curve(conn, "c", closes_fn=stub_closes)
    by = dict(curve)
    assert abs(by["2026-01-02"] - 10000) < 1e-6  # cash 9000 + 10*100
    assert abs(by["2026-01-03"] - 10100) < 1e-6  # 9000 + 10*110
    assert abs(by["2026-01-04"] - 9900) < 1e-6  # 9000 + 10*90
    m = pt.performance_metrics(
        [(d, by[d]) for d in ["2026-01-02", "2026-01-03", "2026-01-04"]]
    )
    assert m["start_eq"] == 10000 and abs(m["end_eq"] - 9900) < 1e-6
    assert abs(m["total"] - (-0.01)) < 1e-9
    assert m["mdd"] < 0  # drew down from 10100 peak

assert pt.sparkline([1, 2, 3, 4]) and len(pt.sparkline([1, 2, 3, 4])) == 4

# --- shared account helper + fresh-account graph + close-position path ---
os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")
importlib.reload(pt)
prices = {"AAPL": 100.0}
conn = pt.db()
made_default = pt.create_account(conn, "fresh", 25_000)
assert made_default
assert (
    conn.execute("SELECT value FROM config WHERE key='default_account'").fetchone()[0]
    == "fresh"
)
assert (
    conn.execute("SELECT COUNT(*) FROM cashflow WHERE account='fresh'").fetchone()[0]
    == 1
)
curve = pt.equity_curve(conn, "fresh", closes_fn=lambda *_: {}, live=True)
assert len(curve) == 2 and curve[0][1] == curve[1][1] == 25_000
pt.place(conn, "fresh", "AAPL", "buy", 2, None, price_fn=fake)
pt.close_position(conn, "fresh", "AAPL", price_fn=fake)
assert (
    conn.execute("SELECT COUNT(*) FROM positions WHERE account='fresh'").fetchone()[0]
    == 0
)
assert (
    conn.execute("SELECT COUNT(*) FROM orders WHERE account='fresh'").fetchone()[0] == 2
)

for bad in (
    ("AAPL", "2026-02-30", 100, "C"),
    ("AAPL", "2026-02-20", float("nan"), "C"),
    ("AAPL", "2026-02-20", 100, "X"),
):
    try:
        pt.build_occ(*bad)
        raise AssertionError(f"invalid option accepted: {bad}")
    except SystemExit:
        pass
conn.close()

# --- idempotency, attribution, previews, and configurable risk controls ---
os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")
importlib.reload(pt)
prices = {"AAPL": 100.0, "AAPL270115C00100000": 2.0}
conn = pt.db()
pt.create_account(conn, "safe", 100_000, source="codex", request_id="create-safe")
pt.create_account(conn, "safe", 100_000, source="codex", request_id="create-safe")
assert (
    conn.execute("SELECT COUNT(*) FROM accounts WHERE name='safe'").fetchone()[0] == 1
)
pt.place(
    conn,
    "safe",
    "AAPL",
    "buy",
    1,
    None,
    price_fn=fake,
    source="codex",
    request_id="trade-1",
)
pt.place(
    conn,
    "safe",
    "AAPL",
    "buy",
    1,
    None,
    price_fn=fake,
    source="codex",
    request_id="trade-1",
)
assert conn.execute("SELECT qty FROM positions WHERE account='safe'").fetchone()[0] == 1
assert (
    conn.execute("SELECT COUNT(*) FROM orders WHERE request_id='trade-1'").fetchone()[0]
    == 1
)
try:
    pt.place(
        conn,
        "safe",
        "AAPL",
        "buy",
        2,
        None,
        price_fn=fake,
        source="codex",
        request_id="trade-1",
    )
    raise AssertionError("idempotency key reuse with different input was accepted")
except SystemExit:
    pass
pt.adjust_cash(conn, "safe", 50, source="hermes", request_id="fund-1")
pt.adjust_cash(conn, "safe", 50, source="hermes", request_id="fund-1")
assert acct(conn, "safe")[0] == 99_950
assert (
    conn.execute("SELECT COUNT(*) FROM audit_log WHERE source='hermes'").fetchone()[0]
    == 1
)

before = acct(conn, "safe")
preview = pt.preview_order(conn, "safe", "AAPL", "buy", 10, 100)
assert preview["allowed"] and preview["position_after"] == 11
assert acct(conn, "safe") == before  # dry-run never mutates
pt.set_risk_limits(conn, "safe", allow_short=False, source="codex")
blocked = pt.preview_order(conn, "safe", "AAPL", "sell", 2, 100)
assert not blocked["allowed"] and "short positions" in blocked["reason"]
try:
    pt.place(conn, "safe", "AAPL", "sell", 2, None, price_fn=fake)
    raise AssertionError("disabled short was accepted")
except SystemExit:
    pass
pt.set_risk_limits(
    conn,
    "safe",
    allow_short=True,
    max_gross_leverage=1,
    max_order_notional=500,
)
assert not pt.preview_order(conn, "safe", "AAPL", "buy", 6, 100)["allowed"]

# Naked option writes fail; a covered call succeeds.
pt.create_account(conn, "naked", 100_000, make_default=False)
naked = pt.preview_order(conn, "naked", "AAPL270115C00100000", "sell", 1, 2)
assert not naked["allowed"] and "naked calls" in naked["reason"]
pt.place(conn, "naked", "AAPL", "buy", 100, None, price_fn=fake)
covered = pt.preview_order(conn, "naked", "AAPL270115C00100000", "sell", 1, 2)
assert covered["allowed"]
pt.place(
    conn,
    "naked",
    "AAPL270115C00100000",
    "sell",
    1,
    None,
    price_fn=fake,
)

# Holiday-aware market clock: July 3, 2026 is the observed Independence Day closure.
holiday = pt.market_clock(datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc))
assert not holiday["is_open"] and holiday["source"].startswith("exchange_calendars")

# Dividends and splits are ledgered and exactly-once.
pt.create_account(conn, "actions", 10_000, make_default=False)
pt.place(conn, "actions", "AAPL", "buy", 10, None, price_fn=fake)
yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(
    timespec="seconds"
)
conn.execute("UPDATE orders SET ts=? WHERE account='actions'", (yesterday,))
today = datetime.now(timezone.utc).date().isoformat()


def stub_actions(_symbol, _start, _end):
    return [(today, "split", 2.0), (today, "dividend", 1.0)]


assert pt.sync_corporate_actions(conn, "actions", actions_fn=stub_actions) == 2
qty, avg = conn.execute(
    "SELECT qty,avg_cost FROM positions WHERE account='actions' AND symbol='AAPL'"
).fetchone()
assert (qty, avg) == (20, 50)
assert acct(conn, "actions")[0] == 9_020 and acct(conn, "actions")[2] == 20
assert pt.sync_corporate_actions(conn, "actions", actions_fn=stub_actions) == 0
assert acct(conn, "actions")[0] == 9_020
assert "request_id" in pt.trade_history_csv(conn, "safe")
assert pt.healthcheck(conn)["status"] == "ok"
with tempfile.TemporaryDirectory() as backup_dir:
    assert os.path.exists(pt.backup_database(conn, backup_dir))
conn.close()

# --- v1 -> v5 migration preserves data and expands safely ---
legacy_path = tempfile.mktemp(suffix=".db")
legacy = sqlite3.connect(legacy_path)
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
      filled_price REAL, ts TEXT);
    CREATE TABLE cashflow(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT,
      ts TEXT, amount REAL);
    INSERT INTO accounts(name,cash,deposits) VALUES('legacy',1234,1234);
    PRAGMA user_version=1;
    """
)
legacy.commit()
legacy.close()
os.environ["PAPERTRADE_DB"] = legacy_path
importlib.reload(pt)
conn = pt.db()
assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
assert (
    conn.execute("SELECT cash FROM accounts WHERE name='legacy'").fetchone()[0] == 1234
)
assert {"source", "request_id", "reject_reason"} <= pt._cols(conn, "orders")
assert {"order_type", "stop_price", "time_in_force", "parent_id"} <= pt._cols(
    conn, "orders"
)
assert conn.execute("SELECT 1 FROM risk_settings WHERE account='legacy'").fetchone()
conn.close()
print("all checks passed")
