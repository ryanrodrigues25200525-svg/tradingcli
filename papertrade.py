#!/usr/bin/env python3
"""papertrade — local multi-account paper trading CLI. Data: yfinance. Storage: SQLite.

Asset classes:
  spot     equities, ETFs, crypto (BTC-USD), FX (EURUSD=X)  — mult 1, cash-funded
  future   e.g. ES=F, GC=F, CL=F (see FUTURES)              — contract mult + margin
  option   OCC symbol e.g. AAPL260116C00250000             — mult 100, premium-funded

Commands: new/accounts/use · buy/sell · option buy|sell · chain · tick
          positions/orders/pnl · cancel · deposit/withdraw · reset/rm · dash
"""

import argparse
import csv
import contextlib
import io
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache

DB = os.environ.get("PAPERTRADE_DB", os.path.expanduser("~/.papertrade.db"))

# future symbol -> (contract multiplier, initial margin per contract)
FUTURES = {
    "ES=F": (50, 12000),
    "MES=F": (5, 1200),
    "NQ=F": (20, 18000),
    "MNQ=F": (2, 1800),
    "YM=F": (5, 9000),
    "RTY=F": (50, 8000),
    "CL=F": (1000, 6000),
    "MCL=F": (100, 600),
    "GC=F": (100, 11000),
    "MGC=F": (10, 1100),
    "SI=F": (5000, 11000),
    "HG=F": (25000, 6000),
    "NG=F": (10000, 3500),
    "ZB=F": (1000, 4000),
    "ZN=F": (1000, 2000),
    "ZF=F": (1000, 1200),
    "6E=F": (125000, 3000),
    "6J=F": (12500000, 3000),
    "6B=F": (62500, 2500),
    "ZC=F": (50, 2000),
    "ZS=F": (50, 3000),
    "ZW=F": (50, 2500),
}

OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")
SCHEMA_VERSION = 3
DEFAULT_RISK = {
    "allow_short": True,
    "allow_naked_options": False,
    "max_gross_leverage": 2.0,
    "max_order_notional": None,
}
ORDER_TYPES = {"market", "limit", "stop", "stop_limit", "trailing_stop"}
TIME_IN_FORCE = {"gtc", "day", "ioc", "fok", "opg", "cls"}
ORDER_CLASSES = {"simple", "bracket", "oco", "oto", "mleg"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS accounts(name TEXT PRIMARY KEY, cash REAL NOT NULL,
  deposits REAL DEFAULT 0, realized REAL DEFAULT 0, created TEXT);
CREATE TABLE IF NOT EXISTS positions(account TEXT, symbol TEXT, qty REAL NOT NULL,
  avg_cost REAL NOT NULL, mult REAL DEFAULT 1, asset_class TEXT DEFAULT 'spot',
  margin REAL DEFAULT 0, PRIMARY KEY(account, symbol));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT,
  symbol TEXT, side TEXT, qty REAL, limit_price REAL, status TEXT DEFAULT 'pending',
  filled_price REAL, ts TEXT, source TEXT DEFAULT 'unknown', request_id TEXT,
  reject_reason TEXT, order_type TEXT DEFAULT 'market', stop_price REAL,
  trail_price REAL, trail_percent REAL, hwm REAL, time_in_force TEXT DEFAULT 'gtc',
  extended_hours INTEGER DEFAULT 0, notional REAL, client_order_id TEXT,
  replaced_by INTEGER, parent_id INTEGER, order_class TEXT DEFAULT 'simple',
  triggered INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cashflow(id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT,
  ts TEXT, amount REAL);
CREATE TABLE IF NOT EXISTS risk_settings(account TEXT PRIMARY KEY,
  allow_short INTEGER NOT NULL DEFAULT 1,
  allow_naked_options INTEGER NOT NULL DEFAULT 0,
  max_gross_leverage REAL NOT NULL DEFAULT 2,
  max_order_notional REAL);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
  source TEXT NOT NULL, request_id TEXT, action TEXT NOT NULL, account TEXT, details TEXT);
CREATE TABLE IF NOT EXISTS corporate_actions(id INTEGER PRIMARY KEY AUTOINCREMENT,
  account TEXT NOT NULL, symbol TEXT NOT NULL, action_date TEXT NOT NULL,
  kind TEXT NOT NULL, value REAL NOT NULL, cash_effect REAL DEFAULT 0,
  UNIQUE(account, symbol, action_date, kind));
CREATE TABLE IF NOT EXISTS corporate_sync(account TEXT, symbol TEXT, last_date TEXT NOT NULL,
  PRIMARY KEY(account, symbol));
CREATE TABLE IF NOT EXISTS watchlists(id INTEGER PRIMARY KEY AUTOINCREMENT,
  account TEXT NOT NULL, name TEXT NOT NULL, created TEXT NOT NULL,
  UNIQUE(account, name));
CREATE TABLE IF NOT EXISTS watchlist_symbols(watchlist_id INTEGER NOT NULL,
  symbol TEXT NOT NULL, position INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(watchlist_id, symbol));
CREATE TABLE IF NOT EXISTS option_instructions(account TEXT NOT NULL,
  symbol TEXT NOT NULL, instruction TEXT NOT NULL, qty REAL, ts TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'unknown', request_id TEXT,
  PRIMARY KEY(account, symbol));
"""


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn):
    """Expand older databases in place without discarding account or order data."""
    added = "deposits" not in _cols(conn, "accounts")
    for col, ddl in [
        ("deposits", "REAL DEFAULT 0"),
        ("realized", "REAL DEFAULT 0"),
        ("created", "TEXT"),
    ]:
        if col not in _cols(conn, "accounts"):
            conn.execute(f"ALTER TABLE accounts ADD COLUMN {col} {ddl}")
    for col, ddl in [
        ("mult", "REAL DEFAULT 1"),
        ("asset_class", "TEXT DEFAULT 'spot'"),
        ("margin", "REAL DEFAULT 0"),
    ]:
        if col not in _cols(conn, "positions"):
            conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {ddl}")
    for col, ddl in [
        ("source", "TEXT DEFAULT 'unknown'"),
        ("request_id", "TEXT"),
        ("reject_reason", "TEXT"),
        ("order_type", "TEXT DEFAULT 'market'"),
        ("stop_price", "REAL"),
        ("trail_price", "REAL"),
        ("trail_percent", "REAL"),
        ("hwm", "REAL"),
        ("time_in_force", "TEXT DEFAULT 'gtc'"),
        ("extended_hours", "INTEGER DEFAULT 0"),
        ("notional", "REAL"),
        ("client_order_id", "TEXT"),
        ("replaced_by", "INTEGER"),
        ("parent_id", "INTEGER"),
        ("order_class", "TEXT DEFAULT 'simple'"),
        ("triggered", "INTEGER DEFAULT 0"),
    ]:
        if col not in _cols(conn, "orders"):
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_source_request"
        " ON orders(source,request_id) WHERE request_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_source_request"
        " ON audit_log(source,request_id) WHERE request_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client_id"
        " ON orders(account,client_order_id) WHERE client_order_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_pending ON orders(status,account,id)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_parent ON orders(parent_id)")
    if added:  # reconstruct contributed capital = cash + cost basis of open positions
        for name, cash in conn.execute("SELECT name, cash FROM accounts").fetchall():
            basis = conn.execute(
                "SELECT COALESCE(SUM(qty*avg_cost*mult),0) FROM positions"
                " WHERE account=?",
                (name,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE accounts SET deposits=? WHERE name=?", (cash + basis, name)
            )
    # seed a dated funding event for any account that has none (needed for equity curves)
    for name, deposits in conn.execute(
        "SELECT name, deposits FROM accounts"
    ).fetchall():
        if not conn.execute(
            "SELECT 1 FROM cashflow WHERE account=? LIMIT 1", (name,)
        ).fetchone():
            first = conn.execute(
                "SELECT MIN(ts) FROM orders WHERE account=?", (name,)
            ).fetchone()[0]
            ts = first or datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO cashflow(account, ts, amount) VALUES(?,?,?)",
                (name, ts, deposits),
            )
            conn.execute(
                "UPDATE accounts SET created=? WHERE name=? AND created IS NULL",
                (ts, name),
            )
    conn.execute(
        "INSERT OR IGNORE INTO risk_settings(account) SELECT name FROM accounts"
    )


def db():
    # WAL + busy_timeout + autocommit so multiple agents (Codex, Claude Code, Hermes) share the DB.
    # Mutations must run inside writing() so BEGIN IMMEDIATE serializes read-modify-write.
    conn = sqlite3.connect(DB, timeout=10, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
        with writing(conn):
            # Another process may have completed this while we waited for the lock.
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                migrate(conn)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn


@contextlib.contextmanager
def writing(conn):
    """Exclusive write transaction. BEGIN IMMEDIATE takes the write lock before any read,
    so concurrent agents can't lose updates in a read-modify-write."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _context(source="unknown", request_id=None):
    source = (source or "unknown").strip().lower()[:64]
    request_id = (request_id or "").strip()[:128] or None
    return source, request_id


def _json_details(details):
    return json.dumps(details or {}, sort_keys=True, separators=(",", ":"))


def _idempotent_action(conn, action, account, source, request_id, details):
    """Return True for an exact replay; reject reuse for a different mutation."""
    if not request_id:
        return False
    row = conn.execute(
        "SELECT action,account,details FROM audit_log WHERE source=? AND request_id=?",
        (source, request_id),
    ).fetchone()
    if not row:
        return False
    expected = (action, account, _json_details(details))
    if row != expected:
        raise SystemExit(
            f"idempotency key '{request_id}' was already used for another action"
        )
    return True


def _audit_locked(
    conn, action, account=None, source="unknown", request_id=None, details=None
):
    source, request_id = _context(source, request_id)
    conn.execute(
        "INSERT INTO audit_log(ts,source,request_id,action,account,details)"
        " VALUES(?,?,?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source,
            request_id,
            action,
            account,
            _json_details(details),
        ),
    )


def risk_limits(conn, account):
    if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (account,)).fetchone():
        raise SystemExit(f"no account '{account}'")
    row = conn.execute(
        "SELECT allow_short,allow_naked_options,max_gross_leverage,max_order_notional"
        " FROM risk_settings WHERE account=?",
        (account,),
    ).fetchone()
    if not row:
        return dict(DEFAULT_RISK)
    return {
        "allow_short": bool(row[0]),
        "allow_naked_options": bool(row[1]),
        "max_gross_leverage": float(row[2]),
        "max_order_notional": float(row[3]) if row[3] is not None else None,
    }


def set_risk_limits(
    conn,
    account,
    allow_short=None,
    allow_naked_options=None,
    max_gross_leverage=None,
    max_order_notional=None,
    clear_max_order=False,
    source="cli",
    request_id=None,
):
    source, request_id = _context(source, request_id)
    details = {
        "allow_short": allow_short,
        "allow_naked_options": allow_naked_options,
        "max_gross_leverage": max_gross_leverage,
        "max_order_notional": max_order_notional,
        "clear_max_order": bool(clear_max_order),
    }
    if max_gross_leverage is not None and (
        not math.isfinite(max_gross_leverage) or max_gross_leverage < 1
    ):
        raise SystemExit("max gross leverage must be a finite number of at least 1")
    if max_order_notional is not None and (
        not math.isfinite(max_order_notional) or max_order_notional <= 0
    ):
        raise SystemExit("max order notional must be a positive finite number")
    with writing(conn):
        if _idempotent_action(conn, "risk.set", account, source, request_id, details):
            print(f"idempotent replay: risk settings for {account}")
            return risk_limits(conn, account)
        current = risk_limits(conn, account)
        updated = {
            "allow_short": current["allow_short"]
            if allow_short is None
            else bool(allow_short),
            "allow_naked_options": current["allow_naked_options"]
            if allow_naked_options is None
            else bool(allow_naked_options),
            "max_gross_leverage": current["max_gross_leverage"]
            if max_gross_leverage is None
            else float(max_gross_leverage),
            "max_order_notional": (
                None
                if clear_max_order
                else current["max_order_notional"]
                if max_order_notional is None
                else float(max_order_notional)
            ),
        }
        conn.execute(
            "INSERT OR REPLACE INTO risk_settings"
            "(account,allow_short,allow_naked_options,max_gross_leverage,max_order_notional)"
            " VALUES(?,?,?,?,?)",
            (
                account,
                int(updated["allow_short"]),
                int(updated["allow_naked_options"]),
                updated["max_gross_leverage"],
                updated["max_order_notional"],
            ),
        )
        _audit_locked(conn, "risk.set", account, source, request_id, details)
    print(
        f"risk {account}: short={updated['allow_short']} "
        f"naked_options={updated['allow_naked_options']} "
        f"max_leverage={updated['max_gross_leverage']:g} "
        f"max_order={updated['max_order_notional'] or 'none'}"
    )
    return updated


def classify(symbol):
    """-> (asset_class, multiplier, margin_per_contract)."""
    symbol = symbol.upper()
    if OCC_RE.match(symbol):
        return "option", 100.0, 0.0
    if symbol in FUTURES:
        mult, margin = FUTURES[symbol]
        return "future", float(mult), float(margin)
    return "spot", 1.0, 0.0


def parse_occ(occ):
    m = OCC_RE.match(occ.upper())
    if not m:
        raise SystemExit(f"bad option symbol '{occ}'")
    root, yy, mm, dd, cp, strike = m.groups()
    return root, f"20{yy}-{mm}-{dd}", int(strike) / 1000.0, cp


def build_occ(root, expiry, strike, cp):
    root, cp = root.strip().upper(), cp.upper()
    if not re.fullmatch(r"[A-Z]{1,6}", root):
        raise SystemExit("option underlying must be 1-6 letters")
    if cp not in ("C", "P"):
        raise SystemExit("option kind must be C or P")
    if not math.isfinite(strike) or strike <= 0:
        raise SystemExit("option strike must be a positive finite number")
    try:
        expiry_date = datetime.strptime(expiry, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(
            "option expiry must be a real date in YYYY-MM-DD format"
        ) from None
    return f"{root}{expiry_date:%y%m%d}{cp}{int(round(strike * 1000)):08d}"


def option_price(occ):
    root, expiry, strike, cp = parse_occ(occ)
    import yfinance as yf

    tk = yf.Ticker(root)
    exps = list(tk.options)
    if expiry not in exps:
        raise SystemExit(
            f"{root} has no {expiry} expiry — available: {', '.join(exps[:8])}"
        )
    df = tk.option_chain(expiry)
    df = df.calls if cp == "C" else df.puts
    rows = df[df.strike == strike]
    if rows.empty:
        raise SystemExit(f"no {strike:g} strike for {root} {expiry}")
    r = rows.iloc[0]
    bid, ask, last = float(r.bid), float(r.ask), float(r.lastPrice)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    if mid <= 0:
        raise SystemExit(f"no tradeable price for {occ}")
    return mid


def _quiet_yf():
    import logging

    for name in ("yfinance", "urllib3", "peewee"):
        logging.getLogger(name).setLevel(logging.CRITICAL)


def live_price(symbol):
    symbol = symbol.upper()
    _quiet_yf()
    if OCC_RE.match(symbol):
        return option_price(symbol)
    import yfinance as yf

    try:
        p = yf.Ticker(symbol).fast_info["lastPrice"]
    except Exception:
        p = None
    if not p or p <= 0:
        raise SystemExit(f"no price for {symbol} (unknown or delisted symbol?)")
    return float(p)


def _apply(state, symbol, side, qty, price):
    """Pure fill core. Mutates state={'cash','realized','pos':{sym:{qty,avg,mult,ac,margin}}}.
    Shared by the DB fill() and the equity-curve replay so the money math is identical."""
    if side not in ("buy", "sell"):
        raise SystemExit("side must be buy or sell")
    ac, mult, margin_per = classify(symbol)
    cash = state["cash"]
    p = state["pos"].get(symbol)
    old_qty, old_avg, old_margin = (
        (p["qty"], p["avg"], p["margin"]) if p else (0.0, 0.0, 0.0)
    )
    signed = qty if side == "buy" else -qty
    new_qty = old_qty + signed
    realized = new_margin = 0.0

    if ac == "future":
        increasing = old_qty == 0 or (old_qty > 0) == (signed > 0)
        if increasing:
            add = abs(signed) * margin_per
            if add > cash:
                raise SystemExit(
                    f"insufficient margin: need {add:,.2f}, have {cash:,.2f}"
                )
            cash -= add
            new_margin = old_margin + add
            new_avg = (old_qty * old_avg + signed * price) / new_qty if new_qty else 0.0
        else:  # reducing / closing / flipping
            closed = min(abs(signed), abs(old_qty))
            direction = 1 if old_qty > 0 else -1
            realized = closed * mult * (price - old_avg) * direction
            release = old_margin * (closed / abs(old_qty))
            cash += release + realized
            new_margin, new_avg = old_margin - release, old_avg
            if abs(signed) > abs(old_qty):  # flip through zero
                rem = abs(signed) - abs(old_qty)
                add = rem * margin_per
                if add > cash:
                    raise SystemExit(
                        f"insufficient margin to flip: need {add:,.2f}, have {cash:,.2f}"
                    )
                cash -= add
                new_margin, new_avg = add, price
    else:  # spot / option — signed notional
        cost = signed * mult * price
        if side == "buy" and cost > cash:
            raise SystemExit(f"insufficient cash: need {cost:,.2f}, have {cash:,.2f}")
        if old_qty != 0 and (old_qty > 0) != (signed > 0):
            closed = min(abs(signed), abs(old_qty))
            direction = 1 if old_qty > 0 else -1
            realized = closed * mult * (price - old_avg) * direction
        if old_qty * signed >= 0 and new_qty != 0:
            new_avg = (old_qty * old_avg + signed * price) / new_qty
        elif abs(signed) > abs(old_qty):
            # A trade that crosses zero opens the remainder at this fill price.
            new_avg = price
        else:
            new_avg = old_avg if new_qty != 0 else 0.0
        cash -= cost

    state["cash"] = cash
    state["realized"] += realized
    if abs(new_qty) < 1e-9:
        state["pos"].pop(symbol, None)
    else:
        state["pos"][symbol] = {
            "qty": new_qty,
            "avg": new_avg,
            "mult": mult,
            "ac": ac,
            "margin": new_margin,
        }


def _portfolio_state_locked(conn, account):
    account_row = conn.execute(
        "SELECT cash FROM accounts WHERE name=?", (account,)
    ).fetchone()
    if not account_row:
        raise SystemExit(f"no account '{account}' — create it first")
    (cash,) = account_row
    pos = {}
    for symbol, q, a, m, ac, mg in conn.execute(
        "SELECT symbol,qty,avg_cost,mult,asset_class,margin"
        " FROM positions WHERE account=?",
        (account,),
    ):
        pos[symbol] = {"qty": q, "avg": a, "mult": m, "ac": ac, "margin": mg}
    return {"cash": cash, "realized": 0.0, "pos": pos}


def _clone_state(state):
    return {
        "cash": state["cash"],
        "realized": state["realized"],
        "pos": {symbol: dict(position) for symbol, position in state["pos"].items()},
    }


def _exposure(state, target_symbol, target_price):
    equity, gross = state["cash"], 0.0
    for symbol, position in state["pos"].items():
        mark = target_price if symbol == target_symbol else position["avg"]
        qty, mult, ac = position["qty"], position["mult"], position["ac"]
        if ac == "future":
            equity += qty * mult * (mark - position["avg"]) + position["margin"]
            continue  # futures are controlled by contract margin, not spot gross leverage
        value = qty * mult * mark
        equity += value
        gross += abs(value)
    return equity, gross


def _risk_report_locked(conn, account, symbol, side, qty, price, before, after):
    limits = risk_limits(conn, account)
    old_qty = before["pos"].get(symbol, {}).get("qty", 0.0)
    new_qty = after["pos"].get(symbol, {}).get("qty", 0.0)
    increasing = abs(new_qty) > abs(old_qty) + 1e-9
    ac, mult, _margin = classify(symbol)
    notional = qty * mult * price
    reason = None

    if new_qty < 0 and abs(new_qty) > abs(min(old_qty, 0.0)) + 1e-9:
        if not limits["allow_short"]:
            reason = "short positions are disabled for this account"
        elif ac == "option" and not limits["allow_naked_options"]:
            root, _expiry, strike, cp = parse_occ(symbol)
            if cp == "C":
                shares = max(0.0, after["pos"].get(root, {}).get("qty", 0.0))
                required = sum(
                    abs(p["qty"]) * 100
                    for other, p in after["pos"].items()
                    if p["ac"] == "option"
                    and p["qty"] < 0
                    and parse_occ(other)[0] == root
                    and parse_occ(other)[3] == "C"
                )
                if shares + 1e-9 < required:
                    reason = (
                        f"naked calls disabled: need {required:g} {root} shares, "
                        f"have {shares:g}"
                    )
            else:
                required = sum(
                    abs(p["qty"]) * parse_occ(other)[2] * 100
                    for other, p in after["pos"].items()
                    if p["ac"] == "option"
                    and p["qty"] < 0
                    and parse_occ(other)[0] == root
                    and parse_occ(other)[3] == "P"
                )
                if after["cash"] + 1e-9 < required:
                    reason = (
                        f"naked puts disabled: need {required:,.2f} cash-secured, "
                        f"have {after['cash']:,.2f}"
                    )

    if (
        not reason
        and increasing
        and limits["max_order_notional"] is not None
        and notional > limits["max_order_notional"] + 1e-9
    ):
        reason = (
            f"order notional {notional:,.2f} exceeds limit "
            f"{limits['max_order_notional']:,.2f}"
        )

    equity_before, gross_before = _exposure(before, symbol, price)
    equity_after, gross_after = _exposure(after, symbol, price)
    leverage = gross_after / equity_after if equity_after > 0 else math.inf
    if (
        not reason
        and gross_after > gross_before + 1e-9
        and leverage > limits["max_gross_leverage"] + 1e-9
    ):
        reason = (
            f"gross leverage {leverage:.2f}x exceeds limit "
            f"{limits['max_gross_leverage']:.2f}x"
        )
    return {
        "allowed": reason is None,
        "reason": reason,
        "account": account,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "notional": notional,
        "cash_before": before["cash"],
        "cash_after": after["cash"],
        "position_before": old_qty,
        "position_after": new_qty,
        "equity_before": equity_before,
        "equity_after": equity_after,
        "gross_before": gross_before,
        "gross_after": gross_after,
        "gross_leverage_after": leverage,
        "limits": limits,
    }


def _preview_locked(conn, account, symbol, side, qty, price):
    before = _portfolio_state_locked(conn, account)
    after = _clone_state(before)
    try:
        _apply(after, symbol, side, qty, price)
    except SystemExit as exc:
        return {
            "allowed": False,
            "reason": str(exc),
            "account": account,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "notional": qty * classify(symbol)[1] * price,
            "cash_before": before["cash"],
            "cash_after": before["cash"],
            "limits": risk_limits(conn, account),
        }
    return _risk_report_locked(conn, account, symbol, side, qty, price, before, after)


def preview_order(conn, account, symbol, side, qty, price=None, price_fn=live_price):
    symbol = symbol.strip().upper()
    if side not in ("buy", "sell"):
        raise SystemExit("side must be buy or sell")
    if not symbol:
        raise SystemExit("symbol required")
    if not math.isfinite(qty) or qty <= 0:
        raise SystemExit("quantity must be a positive finite number")
    if price is None:
        price = price_fn(symbol)
    if not math.isfinite(price) or price <= 0:
        raise SystemExit("preview price must be a positive finite number")
    with writing(conn):
        return _preview_locked(conn, account, symbol, side, qty, price)


def _fill_locked(conn, account, symbol, side, qty, price, enforce_risk=True):
    """Apply a fill while the caller owns a writing() transaction."""
    before = _portfolio_state_locked(conn, account)
    state = _clone_state(before)
    _apply(state, symbol, side, qty, price)
    report = _risk_report_locked(conn, account, symbol, side, qty, price, before, state)
    if enforce_risk and not report["allowed"]:
        raise SystemExit(f"risk rejected: {report['reason']}")
    if symbol in state["pos"]:
        p = state["pos"][symbol]
        conn.execute(
            "INSERT OR REPLACE INTO positions"
            "(account,symbol,qty,avg_cost,mult,asset_class,margin) VALUES(?,?,?,?,?,?,?)",
            (account, symbol, p["qty"], p["avg"], p["mult"], p["ac"], p["margin"]),
        )
    else:
        conn.execute(
            "DELETE FROM positions WHERE account=? AND symbol=?", (account, symbol)
        )
    conn.execute(
        "UPDATE accounts SET cash=?, realized=realized+? WHERE name=?",
        (state["cash"], state["realized"], account),
    )
    return report


def fill(conn, account, symbol, side, qty, price):
    """Atomically apply a fill. Safe when several processes share the database."""
    symbol = symbol.upper()
    if not symbol:
        raise SystemExit("symbol required")
    if not math.isfinite(qty) or qty <= 0:
        raise SystemExit("quantity must be a positive finite number")
    if classify(symbol)[0] == "option" and not math.isclose(qty, round(qty)):
        raise SystemExit("option quantity must be a whole number")
    if not math.isfinite(price) or price < 0:
        raise SystemExit("fill price must be a non-negative finite number")
    with writing(conn):
        _fill_locked(conn, account, symbol, side, qty, price)


def _existing_order(conn, source, request_id, intent, close=False):
    if not request_id:
        return None
    row = conn.execute(
        "SELECT id,account,symbol,side,qty,limit_price,status,filled_price"
        " FROM orders WHERE source=? AND request_id=?",
        (source, request_id),
    ).fetchone()
    if not row:
        if conn.execute(
            "SELECT 1 FROM audit_log WHERE source=? AND request_id=?",
            (source, request_id),
        ).fetchone():
            raise SystemExit(
                f"idempotency key '{request_id}' was already used for another action"
            )
        return None
    oid, account, symbol, side, qty, limit, status, filled = row
    audit = conn.execute(
        "SELECT action FROM audit_log WHERE source=? AND request_id=?",
        (source, request_id),
    ).fetchone()
    expected_action = "order.close" if close else "order.place"
    if audit and audit[0] != expected_action:
        raise SystemExit(
            f"idempotency key '{request_id}' was already used for another action"
        )
    if close:
        matches = (
            account == intent["account"]
            and symbol == intent["symbol"]
            and ("qty" not in intent or math.isclose(qty, intent["qty"]))
        )
    else:
        matches = (
            account == intent["account"]
            and symbol == intent["symbol"]
            and side == intent["side"]
            and math.isclose(qty, intent["qty"])
            and (
                (limit is None and intent["limit"] is None)
                or (
                    limit is not None
                    and intent["limit"] is not None
                    and math.isclose(limit, intent["limit"])
                )
            )
        )
    if not matches:
        raise SystemExit(
            f"idempotency key '{request_id}' was already used for different order #{oid}"
        )
    return {
        "id": oid,
        "account": account,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "limit": limit,
        "status": status,
        "filled_price": filled,
    }


def _print_replayed_order(order):
    price = (
        f" @ {order['filled_price']:.2f}"
        if order["filled_price"] is not None
        else f" limit {order['limit']:.2f}"
        if order["limit"] is not None
        else ""
    )
    print(
        f"idempotent replay: #{order['id']} {order['side']} {order['qty']:g} "
        f"{order['symbol']}{price} [{order['status']}]"
    )


def _insert_order_locked(
    conn,
    account,
    symbol,
    side,
    qty,
    limit,
    status,
    filled_price,
    ts,
    source,
    request_id,
    reject_reason=None,
    order_type=None,
    stop_price=None,
    trail_price=None,
    trail_percent=None,
    hwm=None,
    time_in_force="gtc",
    extended_hours=False,
    notional=None,
    client_order_id=None,
    replaced_by=None,
    parent_id=None,
    order_class="simple",
    triggered=False,
):
    order_type = order_type or ("limit" if limit is not None else "market")
    cur = conn.execute(
        "INSERT INTO orders(account,symbol,side,qty,limit_price,status,filled_price,ts,"
        "source,request_id,reject_reason,order_type,stop_price,trail_price,trail_percent,"
        "hwm,time_in_force,extended_hours,notional,client_order_id,replaced_by,parent_id,"
        "order_class,triggered) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            account,
            symbol,
            side,
            qty,
            limit,
            status,
            filled_price,
            ts,
            source,
            request_id,
            reject_reason,
            order_type,
            stop_price,
            trail_price,
            trail_percent,
            hwm,
            time_in_force,
            int(bool(extended_hours)),
            notional,
            client_order_id,
            replaced_by,
            parent_id,
            order_class,
            int(bool(triggered)),
        ),
    )
    return cur.lastrowid


def place(
    conn,
    account,
    symbol,
    side,
    qty,
    limit,
    price_fn=live_price,
    source="cli",
    request_id=None,
):
    source, request_id = _context(source, request_id)
    if side not in ("buy", "sell"):
        raise SystemExit("side must be buy or sell")
    if not math.isfinite(qty) or qty <= 0:
        raise SystemExit("quantity must be a positive finite number")
    symbol = symbol.upper()
    if not symbol:
        raise SystemExit("symbol required")
    if classify(symbol)[0] == "option" and not math.isclose(qty, round(qty)):
        raise SystemExit("option quantity must be a whole number")
    if limit is not None and (not math.isfinite(limit) or limit <= 0):
        raise SystemExit("limit price must be a positive finite number")
    intent = {
        "account": account,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "limit": limit,
    }
    existing = _existing_order(conn, source, request_id, intent)
    if existing:
        _print_replayed_order(existing)
        return existing["id"]
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if limit is None:
        price = price_fn(symbol)  # network fetch OUTSIDE the write lock
        if not math.isfinite(price) or price <= 0:
            raise SystemExit(f"no valid price for {symbol}")
        with writing(conn):
            existing = _existing_order(conn, source, request_id, intent)
            if existing:
                _print_replayed_order(existing)
                return existing["id"]
            _fill_locked(conn, account, symbol, side, qty, price)
            oid = _insert_order_locked(
                conn,
                account,
                symbol,
                side,
                qty,
                None,
                "filled",
                price,
                ts,
                source,
                request_id,
            )
            _audit_locked(
                conn,
                "order.place",
                account,
                source,
                request_id,
                {**intent, "order_id": oid, "filled_price": price},
            )
        print(f"filled #{oid} {side} {qty:g} {symbol} @ {price:.2f}")
    else:
        with writing(conn):
            existing = _existing_order(conn, source, request_id, intent)
            if existing:
                _print_replayed_order(existing)
                return existing["id"]
            if not conn.execute(
                "SELECT 1 FROM accounts WHERE name=?", (account,)
            ).fetchone():
                raise SystemExit(f"no account '{account}' — create it first")
            _require_available_cash_locked(conn, account, symbol, side, qty, limit)
            preview = _preview_locked(conn, account, symbol, side, qty, limit)
            if not preview["allowed"]:
                raise SystemExit(f"risk rejected: {preview['reason']}")
            oid = _insert_order_locked(
                conn,
                account,
                symbol,
                side,
                qty,
                limit,
                "pending",
                None,
                ts,
                source,
                request_id,
            )
            _audit_locked(
                conn,
                "order.place",
                account,
                source,
                request_id,
                {**intent, "order_id": oid},
            )
        print(
            f"pending #{oid} {side} {qty:g} {symbol} limit {limit:.2f} "
            "(run 'tick' to check fills)"
        )
    return oid


def _positive(value, label):
    if value is None or not math.isfinite(value) or value <= 0:
        raise SystemExit(f"{label} must be a positive finite number")
    return float(value)


def _clean_order_type(value):
    value = (value or "market").strip().lower().replace("-", "_")
    if value not in ORDER_TYPES:
        raise SystemExit(f"order type must be one of: {', '.join(sorted(ORDER_TYPES))}")
    return value


def _order_dict(row):
    if not row:
        return None
    keys = (
        "id account symbol side qty limit_price status filled_price ts source request_id "
        "reject_reason order_type stop_price trail_price trail_percent hwm time_in_force "
        "extended_hours notional client_order_id replaced_by parent_id order_class triggered"
    ).split()
    result = dict(zip(keys, row))
    result["extended_hours"] = bool(result["extended_hours"])
    result["triggered"] = bool(result["triggered"])
    return result


def get_order(conn, order_id=None, client_order_id=None, account=None):
    """Return one order and its directly linked children as JSON-friendly data."""
    columns = (
        "id,account,symbol,side,qty,limit_price,status,filled_price,ts,source,request_id,"
        "reject_reason,order_type,stop_price,trail_price,trail_percent,hwm,time_in_force,"
        "extended_hours,notional,client_order_id,replaced_by,parent_id,order_class,triggered"
    )
    if order_id is not None:
        row = conn.execute(
            f"SELECT {columns} FROM orders WHERE id=?", (order_id,)
        ).fetchone()
    elif client_order_id:
        if not account:
            raise SystemExit("account is required with client order id")
        row = conn.execute(
            f"SELECT {columns} FROM orders WHERE account=? AND client_order_id=?",
            (account, client_order_id),
        ).fetchone()
    else:
        raise SystemExit("order id or client order id required")
    order = _order_dict(row)
    if not order:
        raise SystemExit("order not found")
    children = conn.execute(
        f"SELECT {columns} FROM orders WHERE parent_id=? ORDER BY id", (order["id"],)
    ).fetchall()
    order["children"] = [_order_dict(child) for child in children]
    return order


def _linked_exit_orders_locked(
    conn,
    parent_id,
    account,
    symbol,
    entry_side,
    qty,
    take_profit,
    stop_loss,
    status,
    ts,
    source,
    order_class,
):
    """Create held/active exit legs for bracket and OTO entry orders."""
    exit_side = "sell" if entry_side == "buy" else "buy"
    ids = []
    if take_profit is not None:
        price = (
            take_profit.get("limit_price")
            if isinstance(take_profit, dict)
            else take_profit
        )
        price = _positive(price, "take-profit limit price")
        ids.append(
            _insert_order_locked(
                conn,
                account,
                symbol,
                exit_side,
                qty,
                price,
                status,
                None,
                ts,
                source,
                None,
                order_type="limit",
                parent_id=parent_id,
                order_class=order_class,
            )
        )
    if stop_loss is not None:
        if isinstance(stop_loss, dict):
            stop = stop_loss.get("stop_price")
            limit = stop_loss.get("limit_price")
        else:
            stop, limit = stop_loss, None
        stop = _positive(stop, "stop-loss stop price")
        if limit is not None:
            limit = _positive(limit, "stop-loss limit price")
        ids.append(
            _insert_order_locked(
                conn,
                account,
                symbol,
                exit_side,
                qty,
                limit,
                status,
                None,
                ts,
                source,
                None,
                order_type="stop_limit" if limit else "stop",
                stop_price=stop,
                parent_id=parent_id,
                order_class=order_class,
            )
        )
    return ids


def _reserved_cash_locked(conn, account, exclude_order_id=None):
    """Approximate buying power held by active opening orders."""
    reserved = 0.0
    for oid, symbol, side, qty, limit, stop, hwm in conn.execute(
        "SELECT id,symbol,side,qty,limit_price,stop_price,hwm FROM orders"
        " WHERE account=? AND status='pending'",
        (account,),
    ):
        if oid == exclude_order_id:
            continue
        asset_class, multiplier, margin = classify(symbol)
        if asset_class == "future":
            reserved += abs(qty) * margin
        elif side == "buy":
            reference = limit or stop or hwm or 0.0
            reserved += qty * multiplier * reference
    return reserved


def _require_available_cash_locked(
    conn, account, symbol, side, qty, price, exclude_order_id=None
):
    asset_class, multiplier, margin = classify(symbol)
    required = (
        qty * margin
        if asset_class == "future"
        else qty * multiplier * price
        if side == "buy"
        else 0.0
    )
    if required <= 0:
        return
    cash = conn.execute(
        "SELECT cash FROM accounts WHERE name=?", (account,)
    ).fetchone()[0]
    available = cash - _reserved_cash_locked(conn, account, exclude_order_id)
    if required > available + 1e-9:
        raise SystemExit(
            f"insufficient buying power after open orders: need {required:,.2f}, "
            f"available {available:,.2f}"
        )


def submit_order(
    conn,
    account,
    symbol,
    side,
    qty=None,
    notional=None,
    order_type="market",
    limit_price=None,
    stop_price=None,
    trail_price=None,
    trail_percent=None,
    time_in_force="gtc",
    extended_hours=False,
    client_order_id=None,
    order_class="simple",
    take_profit=None,
    stop_loss=None,
    dry_run=False,
    price_fn=live_price,
    source="cli",
    request_id=None,
):
    """Submit an Alpaca-style simulated order with durable lifecycle metadata."""
    source, request_id = _context(source, request_id)
    symbol = (symbol or "").strip().upper()
    side = (side or "").strip().lower()
    order_type = _clean_order_type(order_type)
    time_in_force = (time_in_force or "gtc").strip().lower()
    order_class = (order_class or "simple").strip().lower()
    client_order_id = (client_order_id or "").strip()[:128] or None
    if not symbol:
        raise SystemExit("symbol required")
    if side not in ("buy", "sell"):
        raise SystemExit("side must be buy or sell")
    if time_in_force not in TIME_IN_FORCE:
        raise SystemExit(
            f"time in force must be one of: {', '.join(sorted(TIME_IN_FORCE))}"
        )
    if order_class not in ORDER_CLASSES - {"mleg"}:
        raise SystemExit("order class must be simple, bracket, oco, or oto")
    if (qty is None) == (notional is None):
        raise SystemExit("provide exactly one of quantity or notional")
    if qty is not None:
        qty = _positive(qty, "quantity")
    if notional is not None:
        notional = _positive(notional, "notional")
        if classify(symbol)[0] != "spot":
            raise SystemExit("notional orders are supported only for spot assets")
    if limit_price is not None:
        limit_price = _positive(limit_price, "limit price")
    if stop_price is not None:
        stop_price = _positive(stop_price, "stop price")
    if trail_price is not None:
        trail_price = _positive(trail_price, "trail price")
    if trail_percent is not None:
        trail_percent = _positive(trail_percent, "trail percent")
    if order_type in ("limit", "stop_limit") and limit_price is None:
        raise SystemExit(f"{order_type.replace('_', '-')} order requires a limit price")
    if order_type in ("stop", "stop_limit") and stop_price is None:
        raise SystemExit(f"{order_type.replace('_', '-')} order requires a stop price")
    if order_type == "trailing_stop" and (trail_price is None) == (
        trail_percent is None
    ):
        raise SystemExit(
            "trailing stop requires exactly one of trail price or trail percent"
        )
    if order_type != "trailing_stop" and (
        trail_price is not None or trail_percent is not None
    ):
        raise SystemExit("trail values are valid only for trailing-stop orders")
    if extended_hours and (
        order_type != "limit" or time_in_force not in ("day", "gtc")
    ):
        raise SystemExit("extended hours requires a day or gtc limit order")
    if time_in_force in ("ioc", "fok") and order_type not in ("market", "limit"):
        raise SystemExit("ioc/fok is supported only for market and limit orders")
    if time_in_force in ("ioc", "fok") and order_class != "simple":
        raise SystemExit("ioc/fok is not supported for linked orders")
    if order_class == "bracket" and (take_profit is None or stop_loss is None):
        raise SystemExit("bracket order requires take-profit and stop-loss")
    if order_class == "oto" and (take_profit is None) == (stop_loss is None):
        raise SystemExit("oto order requires exactly one exit leg")
    if order_class == "oco" and (take_profit is None or stop_loss is None):
        raise SystemExit("oco order requires take-profit and stop-loss")

    requested_qty = qty
    intent = {
        "account": account,
        "symbol": symbol,
        "side": side,
        "qty": requested_qty,
        "notional": notional,
        "order_type": order_type,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "trail_price": trail_price,
        "trail_percent": trail_percent,
        "time_in_force": time_in_force,
        "extended_hours": bool(extended_hours),
        "client_order_id": client_order_id,
        "order_class": order_class,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
    }
    if request_id and not dry_run:
        with writing(conn):
            if _idempotent_action(
                conn, "order.submit", account, source, request_id, intent
            ):
                row = conn.execute(
                    "SELECT id FROM orders WHERE source=? AND request_id=?",
                    (source, request_id),
                ).fetchone()
                print(f"idempotent replay: order #{row[0]}")
                return row[0]

    needs_quote = notional is not None or order_type in ("market", "trailing_stop")
    needs_quote = needs_quote or time_in_force in ("ioc", "fok")
    price = price_fn(symbol) if needs_quote else None
    if price is not None and (not math.isfinite(price) or price <= 0):
        raise SystemExit(f"no valid price for {symbol}")
    if notional is not None:
        qty = notional / (classify(symbol)[1] * price)
    if classify(symbol)[0] == "option" and not math.isclose(qty, round(qty)):
        raise SystemExit("option quantity must be a whole number")
    if order_class == "oco":
        position = conn.execute(
            "SELECT qty FROM positions WHERE account=? AND symbol=?", (account, symbol)
        ).fetchone()
        if (
            not position
            or (side == "sell" and position[0] < qty)
            or (side == "buy" and position[0] > -qty)
        ):
            raise SystemExit("oco exits require a sufficient open position")
    validation_price = price or limit_price or stop_price
    if dry_run:
        report = preview_order(
            conn, account, symbol, side, qty, validation_price, price_fn=price_fn
        )
        report.update(
            {
                "dry_run": True,
                "order_type": order_type,
                "order_class": order_class,
                "time_in_force": time_in_force,
                "extended_hours": bool(extended_hours),
                "estimated_qty": qty,
                "requested_notional": notional,
            }
        )
        print(json.dumps(report, indent=2))
        return report
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with writing(conn):
        if _idempotent_action(
            conn, "order.submit", account, source, request_id, intent
        ):
            row = conn.execute(
                "SELECT id FROM orders WHERE source=? AND request_id=?",
                (source, request_id),
            ).fetchone()
            print(f"idempotent replay: order #{row[0]}")
            return row[0]
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE name=?", (account,)
        ).fetchone():
            raise SystemExit(f"no account '{account}' — create it first")
        if (
            client_order_id
            and conn.execute(
                "SELECT 1 FROM orders WHERE account=? AND client_order_id=?",
                (account, client_order_id),
            ).fetchone()
        ):
            raise SystemExit(f"duplicate client order id '{client_order_id}'")

        if order_class == "oco":
            position = conn.execute(
                "SELECT qty FROM positions WHERE account=? AND symbol=?",
                (account, symbol),
            ).fetchone()
            if (
                not position
                or (side == "sell" and position[0] < qty)
                or (side == "buy" and position[0] > -qty)
            ):
                raise SystemExit("oco exits require a sufficient open position")
            tp = (
                take_profit.get("limit_price")
                if isinstance(take_profit, dict)
                else take_profit
            )
            tp = _positive(tp, "take-profit limit price")
            oid = _insert_order_locked(
                conn,
                account,
                symbol,
                side,
                qty,
                tp,
                "pending",
                None,
                ts,
                source,
                request_id,
                order_type="limit",
                time_in_force=time_in_force,
                extended_hours=extended_hours,
                notional=notional,
                client_order_id=client_order_id,
                order_class="oco",
            )
            _linked_exit_orders_locked(
                conn,
                oid,
                account,
                symbol,
                "buy" if side == "sell" else "sell",
                qty,
                None,
                stop_loss,
                "pending",
                ts,
                source,
                "oco",
            )
            _audit_locked(conn, "order.submit", account, source, request_id, intent)
            print(f"pending OCO #{oid} {side} {qty:g} {symbol}")
            return oid

        immediate = order_type == "market" and time_in_force not in ("opg", "cls")
        if order_type == "limit" and time_in_force in ("ioc", "fok"):
            immediate = price <= limit_price if side == "buy" else price >= limit_price
        status = "filled" if immediate else "pending"
        if not immediate and time_in_force in ("ioc", "fok"):
            status = "canceled"
        if status == "pending":
            _require_available_cash_locked(
                conn, account, symbol, side, qty, validation_price
            )
        preview = _preview_locked(conn, account, symbol, side, qty, validation_price)
        if not preview["allowed"]:
            raise SystemExit(f"risk rejected: {preview['reason']}")
        if immediate:
            _fill_locked(conn, account, symbol, side, qty, price)
        oid = _insert_order_locked(
            conn,
            account,
            symbol,
            side,
            qty,
            limit_price,
            status,
            price if immediate else None,
            ts,
            source,
            request_id,
            order_type=order_type,
            stop_price=stop_price,
            trail_price=trail_price,
            trail_percent=trail_percent,
            hwm=price if order_type == "trailing_stop" else None,
            time_in_force=time_in_force,
            extended_hours=extended_hours,
            notional=notional,
            client_order_id=client_order_id,
            order_class=order_class,
        )
        if order_class in ("bracket", "oto"):
            _linked_exit_orders_locked(
                conn,
                oid,
                account,
                symbol,
                side,
                qty,
                take_profit,
                stop_loss,
                "pending" if immediate else "held",
                ts,
                source,
                order_class,
            )
        _audit_locked(conn, "order.submit", account, source, request_id, intent)
    if status == "filled":
        print(f"filled #{oid} {side} {qty:g} {symbol} @ {price:.2f}")
    elif status == "canceled":
        print(f"canceled #{oid}: {time_in_force} order was not immediately marketable")
    else:
        print(f"pending #{oid} {side} {qty:g} {symbol} [{order_type} {time_in_force}]")
    return oid


def replace_order(
    conn,
    order_id,
    qty=None,
    limit_price=None,
    stop_price=None,
    trail=None,
    time_in_force=None,
    client_order_id=None,
    source="cli",
    request_id=None,
):
    """Replace a pending order by creating a new linked order and retiring the old one."""
    source, request_id = _context(source, request_id)
    details = {
        "order_id": int(order_id),
        "qty": qty,
        "limit_price": limit_price,
        "stop_price": stop_price,
        "trail": trail,
        "time_in_force": time_in_force,
        "client_order_id": client_order_id,
    }
    with writing(conn):
        old = get_order(conn, order_id=order_id)
        if _idempotent_action(
            conn, "order.replace", old["account"], source, request_id, details
        ):
            row = conn.execute(
                "SELECT replaced_by FROM orders WHERE id=?", (order_id,)
            ).fetchone()
            print(f"idempotent replay: replacement #{row[0]}")
            return row[0]
        if old["status"] not in ("pending", "held"):
            raise SystemExit(f"order #{order_id} is {old['status']}, not replaceable")
        if old["notional"] is not None:
            raise SystemExit("notional orders cannot be replaced; cancel and resubmit")
        new_qty = old["qty"] if qty is None else _positive(qty, "quantity")
        if classify(old["symbol"])[0] == "option" and not math.isclose(
            new_qty, round(new_qty)
        ):
            raise SystemExit("option quantity must be a whole number")
        new_limit = (
            old["limit_price"]
            if limit_price is None
            else _positive(limit_price, "limit price")
        )
        new_stop = (
            old["stop_price"]
            if stop_price is None
            else _positive(stop_price, "stop price")
        )
        new_tif = (
            old["time_in_force"] if time_in_force is None else time_in_force.lower()
        )
        if new_tif not in TIME_IN_FORCE:
            raise SystemExit("invalid time in force")
        trail_price, trail_percent = old["trail_price"], old["trail_percent"]
        if trail is not None:
            trail = _positive(trail, "trail")
            if trail_price is not None:
                trail_price = trail
            elif trail_percent is not None:
                trail_percent = trail
            else:
                raise SystemExit("trail can replace only a trailing-stop order")
        if (
            client_order_id
            and conn.execute(
                "SELECT 1 FROM orders WHERE account=? AND client_order_id=?",
                (old["account"], client_order_id),
            ).fetchone()
        ):
            raise SystemExit(f"duplicate client order id '{client_order_id}'")
        reference = new_limit or new_stop or old["hwm"]
        if old["status"] == "pending" and reference is not None:
            _require_available_cash_locked(
                conn,
                old["account"],
                old["symbol"],
                old["side"],
                new_qty,
                reference,
                exclude_order_id=order_id,
            )
            preview = _preview_locked(
                conn,
                old["account"],
                old["symbol"],
                old["side"],
                new_qty,
                reference,
            )
            if not preview["allowed"]:
                raise SystemExit(f"risk rejected: {preview['reason']}")
        elif qty is not None and reference is None:
            raise SystemExit("cannot resize an auction market order without a price")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new_id = _insert_order_locked(
            conn,
            old["account"],
            old["symbol"],
            old["side"],
            new_qty,
            new_limit,
            old["status"],
            None,
            ts,
            source,
            request_id,
            order_type=old["order_type"],
            stop_price=new_stop,
            trail_price=trail_price,
            trail_percent=trail_percent,
            hwm=old["hwm"],
            time_in_force=new_tif,
            extended_hours=old["extended_hours"],
            notional=old["notional"],
            client_order_id=(client_order_id or "").strip() or None,
            parent_id=old["parent_id"],
            order_class=old["order_class"],
            triggered=old["triggered"],
        )
        conn.execute(
            "UPDATE orders SET status='replaced',replaced_by=? WHERE id=?",
            (new_id, order_id),
        )
        conn.execute(
            "UPDATE orders SET parent_id=? WHERE parent_id=?", (new_id, order_id)
        )
        _audit_locked(
            conn, "order.replace", old["account"], source, request_id, details
        )
    print(f"replaced #{order_id} with #{new_id}")
    return new_id


def cancel_all_orders(conn, account=None, source="cli", request_id=None):
    source, request_id = _context(source, request_id)
    details = {"account": account}
    with writing(conn):
        if _idempotent_action(
            conn, "order.cancel_all", account, source, request_id, details
        ):
            print("idempotent replay: cancel all")
            return 0
        params = (account,) if account else ()
        where = " AND account=?" if account else ""
        cur = conn.execute(
            "UPDATE orders SET status='canceled' WHERE status IN ('pending','held')"
            + where,
            params,
        )
        _audit_locked(conn, "order.cancel_all", account, source, request_id, details)
    print(f"canceled {cur.rowcount} orders")
    return cur.rowcount


@lru_cache(maxsize=1)
def _nyse_calendar():
    import exchange_calendars as xcals

    return xcals.get_calendar("XNYS")


def market_clock(now=None):
    """NYSE status with holidays, early closes, and the next transition."""
    import pandas as pd
    from zoneinfo import ZoneInfo

    if now is None:
        now = datetime.now(timezone.utc)
    ts = pd.Timestamp(now)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    try:
        calendar = _nyse_calendar()
        is_open = bool(calendar.is_open_on_minute(ts))
        transition = calendar.next_close(ts) if is_open else calendar.next_open(ts)
        previous_close = calendar.previous_close(ts)
        source = "exchange_calendars:XNYS"
    except Exception:
        eastern = ts.to_pydatetime().astimezone(ZoneInfo("America/New_York"))
        is_open = eastern.weekday() < 5 and (9, 30) <= (
            eastern.hour,
            eastern.minute,
        ) < (16, 0)
        transition = None
        previous_close = None
        source = "weekday fallback"

    def stamp(value):
        if value is None:
            return None
        dt = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
        return {
            "utc": dt.astimezone(timezone.utc).isoformat(timespec="minutes"),
            "eastern": dt.astimezone(ZoneInfo("America/New_York")).isoformat(
                timespec="minutes"
            ),
        }

    return {
        "market": "NYSE",
        "is_open": is_open,
        "status": "open" if is_open else "closed",
        "transition": "close" if is_open else "open",
        "next_transition": stamp(transition),
        "previous_close": stamp(previous_close),
        "source": source,
    }


def market_open():
    return market_clock()["is_open"]


def market_calendar(start=None, end=None):
    """Return holiday and early-close-aware NYSE sessions for a bounded date range."""
    from zoneinfo import ZoneInfo

    today = datetime.now(timezone.utc).date()
    try:
        start_date = datetime.strptime(start, "%Y-%m-%d").date() if start else today
        end_date = (
            datetime.strptime(end, "%Y-%m-%d").date()
            if end
            else start_date + timedelta(days=14)
        )
    except ValueError:
        raise SystemExit("calendar dates must use YYYY-MM-DD") from None
    if end_date < start_date:
        raise SystemExit("calendar end must not precede start")
    if (end_date - start_date).days > 366:
        raise SystemExit("calendar range cannot exceed 366 days")
    calendar = _nyse_calendar()
    eastern = ZoneInfo("America/New_York")
    sessions = []
    try:
        for session in calendar.sessions_in_range(start_date, end_date):
            opened = calendar.session_open(session).to_pydatetime()
            closed = calendar.session_close(session).to_pydatetime()
            sessions.append(
                {
                    "date": session.date().isoformat(),
                    "open": opened.astimezone(eastern).isoformat(timespec="minutes"),
                    "close": closed.astimezone(eastern).isoformat(timespec="minutes"),
                    "early_close": closed.astimezone(eastern).hour < 16,
                }
            )
    except Exception as exc:
        raise SystemExit(f"market calendar unavailable: {exc}") from None
    return sessions


def close_position(
    conn,
    account,
    symbol,
    qty=None,
    percent=None,
    price_fn=live_price,
    source="cli",
    request_id=None,
):
    """Close all or part of a position at market by quantity or percentage."""
    source, request_id = _context(source, request_id)
    symbol = symbol.upper()
    existing = _existing_order(
        conn, source, request_id, {"account": account, "symbol": symbol}, close=True
    )
    if existing:
        _print_replayed_order(existing)
        return existing["id"]
    if qty is not None and percent is not None:
        raise SystemExit("provide quantity or percentage, not both")
    row = conn.execute(
        "SELECT qty FROM positions WHERE account=? AND symbol=?", (account, symbol)
    ).fetchone()
    if not row:
        raise SystemExit(f"no open position in {symbol}")
    open_qty = row[0]
    if percent is not None:
        if not math.isfinite(percent) or not 0 < percent <= 100:
            raise SystemExit("percentage must be greater than 0 and at most 100")
        close_qty = abs(open_qty) * percent / 100
    elif qty is not None:
        close_qty = _positive(qty, "quantity")
        if close_qty > abs(open_qty) + 1e-9:
            raise SystemExit(f"cannot close {close_qty:g}; only {abs(open_qty):g} open")
    else:
        close_qty = abs(open_qty)
    if (
        classify(symbol)[0] == "option"
        and not math.isclose(close_qty, round(close_qty))
        and not math.isclose(close_qty, abs(open_qty))
    ):
        raise SystemExit("option close quantity must be a whole number")
    intent = {"account": account, "symbol": symbol, "qty": close_qty}
    existing = _existing_order(conn, source, request_id, intent, close=True)
    if existing:
        _print_replayed_order(existing)
        return existing["id"]
    price = price_fn(symbol)  # network fetch outside the write lock
    if not math.isfinite(price) or price <= 0:
        raise SystemExit(f"no valid price for {symbol}")
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with writing(conn):
        existing = _existing_order(conn, source, request_id, intent, close=True)
        if existing:
            _print_replayed_order(existing)
            return existing["id"]
        row = conn.execute(
            "SELECT qty FROM positions WHERE account=? AND symbol=?", (account, symbol)
        ).fetchone()
        if not row:
            raise SystemExit(f"no open position in {symbol}")
        current_qty = row[0]
        if close_qty > abs(current_qty) + 1e-9:
            raise SystemExit("position changed while close order was being prepared")
        side = "sell" if current_qty > 0 else "buy"
        # A manual liquidation supersedes outstanding orders for this instrument.
        # Cancel them under the same write lock so a protective exit cannot later
        # reopen the position in the opposite direction.
        conn.execute(
            "UPDATE orders SET status='canceled' WHERE account=? AND symbol=?"
            " AND status IN ('pending','held')",
            (account, symbol),
        )
        _fill_locked(conn, account, symbol, side, close_qty, price)
        oid = _insert_order_locked(
            conn,
            account,
            symbol,
            side,
            close_qty,
            None,
            "filled",
            price,
            ts,
            source,
            request_id,
        )
        _audit_locked(
            conn,
            "order.close",
            account,
            source,
            request_id,
            {**intent, "order_id": oid, "filled_price": price},
        )
    print(f"filled #{oid} {side} {close_qty:g} {symbol} @ {price:.2f}")
    return oid


def close_all_positions(
    conn, account, price_fn=live_price, source="cli", request_id=None
):
    """Close every currently open position; each leg remains independently auditable."""
    symbols = [
        symbol
        for (symbol,) in conn.execute(
            "SELECT symbol FROM positions WHERE account=? ORDER BY symbol", (account,)
        )
    ]
    if not symbols:
        print("no positions")
        return []
    ids = []
    for symbol in symbols:
        key = f"{request_id}:{symbol}" if request_id else None
        ids.append(
            close_position(
                conn,
                account,
                symbol,
                price_fn=price_fn,
                source=source,
                request_id=key,
            )
        )
    print(f"closed {len(ids)} positions")
    return ids


def get_position(conn, account, symbol, price_fn=live_price, price=None):
    symbol = symbol.strip().upper()
    row = conn.execute(
        "SELECT qty,avg_cost,mult,asset_class,margin FROM positions"
        " WHERE account=? AND symbol=?",
        (account, symbol),
    ).fetchone()
    if not row:
        raise SystemExit(f"no open position in {symbol}")
    qty, avg, mult, asset_class, margin = row
    if price is None:
        price = price_fn(symbol)
    unrealized = qty * mult * (price - avg)
    market_value = (
        margin + unrealized if asset_class == "future" else qty * mult * price
    )
    return {
        "account": account,
        "symbol": symbol,
        "side": "long" if qty > 0 else "short",
        "qty": abs(qty),
        "signed_qty": qty,
        "avg_entry_price": avg,
        "current_price": price,
        "market_value": market_value,
        "unrealized_pl": unrealized,
        "asset_class": asset_class,
        "multiplier": mult,
        "margin": margin,
    }


def list_positions(conn, account, price_fn=live_price):
    if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (account,)).fetchone():
        raise SystemExit(f"no account '{account}'")
    symbols = [
        symbol
        for (symbol,) in conn.execute(
            "SELECT symbol FROM positions WHERE account=? ORDER BY symbol", (account,)
        )
    ]
    marks = batch_prices(symbols, price_fn=price_fn)
    return [
        get_position(conn, account, symbol, price_fn, price=marks[symbol])
        for symbol in symbols
    ]


def option_contract_details(symbol, price_fn=live_price):
    symbol = symbol.strip().upper()
    root, expiry, strike, kind = parse_occ(symbol)
    result = {
        "symbol": symbol,
        "underlying": root,
        "expiration_date": expiry,
        "strike_price": strike,
        "type": "call" if kind == "C" else "put",
        "style": "american",
        "size": 100,
        "expired": expiry < datetime.now(timezone.utc).date().isoformat(),
    }
    try:
        result["price"] = price_fn(symbol)
        result["tradable"] = True
    except SystemExit as exc:
        result["price"] = None
        result["tradable"] = False
        result["price_error"] = str(exc)
    return result


def exercise_option(conn, account, symbol, qty=None, source="cli", request_id=None):
    """Exercise a long American option into its underlying shares atomically."""
    source, request_id = _context(source, request_id)
    symbol = symbol.strip().upper()
    root, expiry, strike, kind = parse_occ(symbol)
    if request_id:
        prior = conn.execute(
            "SELECT action,account,details FROM audit_log WHERE source=? AND request_id=?",
            (source, request_id),
        ).fetchone()
        if prior:
            saved = json.loads(prior[2] or "{}")
            if (
                prior[0] == "option.exercise"
                and prior[1] == account
                and saved.get("symbol") == symbol
                and (qty is None or math.isclose(float(saved.get("qty", 0)), qty))
            ):
                print(f"idempotent replay: exercised {symbol}")
                return
            raise SystemExit(
                f"idempotency key '{request_id}' was already used for another action"
            )
    row = conn.execute(
        "SELECT qty FROM positions WHERE account=? AND symbol=?", (account, symbol)
    ).fetchone()
    if not row or row[0] <= 0:
        raise SystemExit("a long option position is required for exercise")
    exercise_qty = row[0] if qty is None else _positive(qty, "quantity")
    if not math.isclose(exercise_qty, round(exercise_qty)):
        raise SystemExit("exercise quantity must be a whole number")
    if exercise_qty > row[0] + 1e-9:
        raise SystemExit(f"cannot exercise {exercise_qty:g}; only {row[0]:g} held")
    details = {"symbol": symbol, "qty": exercise_qty}
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with writing(conn):
        if _idempotent_action(
            conn, "option.exercise", account, source, request_id, details
        ):
            print(f"idempotent replay: exercised {symbol}")
            return
        instruction = conn.execute(
            "SELECT instruction FROM option_instructions WHERE account=? AND symbol=?",
            (account, symbol),
        ).fetchone()
        if instruction and instruction[0] == "do_not_exercise":
            raise SystemExit("option is marked do-not-exercise")
        current = conn.execute(
            "SELECT qty FROM positions WHERE account=? AND symbol=?", (account, symbol)
        ).fetchone()
        if not current or current[0] + 1e-9 < exercise_qty:
            raise SystemExit("option position changed while exercise was prepared")
        _fill_locked(
            conn, account, symbol, "sell", exercise_qty, 0.0, enforce_risk=False
        )
        underlying_side = "buy" if kind == "C" else "sell"
        shares = exercise_qty * 100
        _fill_locked(
            conn, account, root, underlying_side, shares, strike, enforce_risk=False
        )
        option_order = _insert_order_locked(
            conn,
            account,
            symbol,
            "sell",
            exercise_qty,
            None,
            "exercised",
            0.0,
            ts,
            source,
            request_id,
            order_type="market",
            order_class="simple",
        )
        _insert_order_locked(
            conn,
            account,
            root,
            underlying_side,
            shares,
            None,
            "exercised",
            strike,
            ts,
            source,
            None,
            order_type="market",
            parent_id=option_order,
            order_class="simple",
        )
        conn.execute(
            "INSERT OR REPLACE INTO option_instructions"
            "(account,symbol,instruction,qty,ts,source,request_id) VALUES(?,?,?,?,?,?,?)",
            (account, symbol, "exercise", exercise_qty, ts, source, request_id),
        )
        _audit_locked(conn, "option.exercise", account, source, request_id, details)
    print(
        f"exercised {exercise_qty:g} {symbol}: {underlying_side} "
        f"{shares:g} {root} @ {strike:.2f}"
    )


def do_not_exercise_option(conn, account, symbol, source="cli", request_id=None):
    source, request_id = _context(source, request_id)
    symbol = symbol.strip().upper()
    parse_occ(symbol)
    details = {"symbol": symbol}
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with writing(conn):
        if _idempotent_action(
            conn, "option.do_not_exercise", account, source, request_id, details
        ):
            print(f"idempotent replay: do-not-exercise {symbol}")
            return
        row = conn.execute(
            "SELECT qty FROM positions WHERE account=? AND symbol=?", (account, symbol)
        ).fetchone()
        if not row or row[0] <= 0:
            raise SystemExit("a long option position is required")
        existing = conn.execute(
            "SELECT instruction FROM option_instructions WHERE account=? AND symbol=?",
            (account, symbol),
        ).fetchone()
        if existing and existing[0] == "exercise":
            raise SystemExit("option has already been exercised")
        conn.execute(
            "INSERT OR REPLACE INTO option_instructions"
            "(account,symbol,instruction,qty,ts,source,request_id) VALUES(?,?,?,?,?,?,?)",
            (account, symbol, "do_not_exercise", row[0], ts, source, request_id),
        )
        _audit_locked(
            conn, "option.do_not_exercise", account, source, request_id, details
        )
    print(f"marked {symbol} do-not-exercise")


def submit_option_multileg(
    conn,
    account,
    legs,
    limit_price=None,
    price_fn=live_price,
    source="cli",
    request_id=None,
    client_order_id=None,
):
    """Atomically fill a two-to-four-leg option strategy at current mid prices."""
    source, request_id = _context(source, request_id)
    if not isinstance(legs, list) or not 2 <= len(legs) <= 4:
        raise SystemExit("multi-leg order requires two to four legs")
    clean = []
    for leg in legs:
        if not isinstance(leg, dict):
            raise SystemExit("each leg must be an object")
        symbol = str(leg.get("symbol", "")).upper()
        parse_occ(symbol)
        side = str(leg.get("side", "")).lower()
        if side not in ("buy", "sell"):
            raise SystemExit("each leg side must be buy or sell")
        try:
            raw_qty = float(leg.get("qty", 0))
        except (TypeError, ValueError):
            raise SystemExit("leg quantity must be a positive finite number") from None
        qty = _positive(raw_qty, "leg quantity")
        if not math.isclose(qty, round(qty)):
            raise SystemExit("option leg quantity must be a whole number")
        clean.append({"symbol": symbol, "side": side, "qty": qty})
    roots = {parse_occ(leg["symbol"])[0] for leg in clean}
    if len(roots) != 1:
        raise SystemExit("all option legs must share one underlying")
    if limit_price is not None:
        limit_price = _positive(limit_price, "net limit price")
    details = {
        "legs": clean,
        "limit_price": limit_price,
        "client_order_id": client_order_id,
    }
    if request_id:
        with writing(conn):
            if _idempotent_action(
                conn, "option.mleg", account, source, request_id, details
            ):
                print("idempotent replay: multi-leg option order")
                return []
    prices = {leg["symbol"]: price_fn(leg["symbol"]) for leg in clean}
    net = sum(
        prices[leg["symbol"]] * leg["qty"] * (1 if leg["side"] == "buy" else -1)
        for leg in clean
    )
    if limit_price is not None and net > limit_price + 1e-9:
        raise SystemExit(
            f"strategy net debit {net:.2f} exceeds limit {limit_price:.2f}"
        )
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with writing(conn):
        if _idempotent_action(
            conn, "option.mleg", account, source, request_id, details
        ):
            print("idempotent replay: multi-leg option order")
            return []
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE name=?", (account,)
        ).fetchone():
            raise SystemExit(f"no account '{account}'")
        ids = []
        # Credit legs first avoids rejecting a valid net-credit/debit package mid-transaction.
        ordered = sorted(clean, key=lambda leg: leg["side"] == "buy")
        for leg in ordered:
            _fill_locked(
                conn,
                account,
                leg["symbol"],
                leg["side"],
                leg["qty"],
                prices[leg["symbol"]],
                enforce_risk=False,
            )
            oid = _insert_order_locked(
                conn,
                account,
                leg["symbol"],
                leg["side"],
                leg["qty"],
                None,
                "filled",
                prices[leg["symbol"]],
                ts,
                source,
                request_id if not ids else None,
                order_type="market",
                client_order_id=client_order_id if not ids else None,
                parent_id=ids[0] if ids else None,
                order_class="mleg",
            )
            ids.append(oid)
        _audit_locked(conn, "option.mleg", account, source, request_id, details)
    print(f"filled multi-leg strategy #{ids[0]} ({len(ids)} legs, net {net:+.2f})")
    return ids


def set_default(conn, name, source="cli", request_id=None):
    source, request_id = _context(source, request_id)
    details = {"name": name}
    with writing(conn):
        if _idempotent_action(
            conn, "account.default", name, source, request_id, details
        ):
            print(f"idempotent replay: default account {name}")
            return
        if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
            raise SystemExit(f"no account '{name}'")
        conn.execute(
            "INSERT OR REPLACE INTO config VALUES('default_account',?)", (name,)
        )
        _audit_locked(conn, "account.default", name, source, request_id, details)
    print(f"default account: {name}")


def cancel(conn, oid, source="cli", request_id=None):
    source, request_id = _context(source, request_id)
    with writing(conn):
        order = conn.execute(
            "SELECT account,status,parent_id,order_class FROM orders WHERE id=?", (oid,)
        ).fetchone()
        account = order[0] if order else None
        details = {"order_id": oid}
        if _idempotent_action(
            conn, "order.cancel", account, source, request_id, details
        ):
            print(f"idempotent replay: canceled #{oid}")
            return
        if not order:
            raise SystemExit(f"no order #{oid}")
        status, parent_id, order_class = order[1:]
        if status not in ("pending", "held"):
            raise SystemExit(f"order #{oid} is {status}, not cancelable")
        if order_class == "oco":
            root = parent_id or oid
            conn.execute(
                "UPDATE orders SET status='canceled' WHERE (id=? OR parent_id=?)"
                " AND status IN ('pending','held')",
                (root, root),
            )
        elif parent_id is None:
            conn.execute(
                "UPDATE orders SET status='canceled' WHERE (id=? OR parent_id=?)"
                " AND status IN ('pending','held')",
                (oid, oid),
            )
        else:
            conn.execute("UPDATE orders SET status='canceled' WHERE id=?", (oid,))
        _audit_locked(conn, "order.cancel", account, source, request_id, details)
    print(f"canceled #{oid}")


def adjust_cash(conn, account, amount, source="cli", request_id=None):
    """Positive = deposit, negative = withdraw. Changes contributed capital, not P&L."""
    if not math.isfinite(amount) or amount == 0:
        raise SystemExit("cash adjustment must be a non-zero finite number")
    source, request_id = _context(source, request_id)
    details = {"amount": amount}
    with writing(conn):
        if _idempotent_action(
            conn, "cash.adjust", account, source, request_id, details
        ):
            print(f"idempotent replay: cash adjustment {amount:+,.2f} for {account}")
            return
        row = conn.execute(
            "SELECT cash FROM accounts WHERE name=?", (account,)
        ).fetchone()
        if not row:
            raise SystemExit(f"no account '{account}'")
        if amount < 0 and row[0] + amount < 0:
            raise SystemExit(f"insufficient cash: have {row[0]:,.2f}")
        conn.execute(
            "UPDATE accounts SET cash=cash+?, deposits=deposits+? WHERE name=?",
            (amount, amount, account),
        )
        conn.execute(
            "INSERT INTO cashflow(account, ts, amount) VALUES(?,?,?)",
            (account, datetime.now(timezone.utc).isoformat(timespec="seconds"), amount),
        )
        new_cash = row[0] + amount
        _audit_locked(conn, "cash.adjust", account, source, request_id, details)
    verb = "deposited" if amount >= 0 else "withdrew"
    print(f"{verb} {abs(amount):,.2f} — cash now {new_cash:,.2f}")


def create_account(
    conn,
    name,
    cash=100_000,
    make_default=True,
    source="cli",
    request_id=None,
):
    """Create a portfolio with a dated funding event. Used by CLI, dashboard, and MCP."""
    name = name.strip()
    if not name:
        raise SystemExit("name required")
    if not math.isfinite(cash) or cash < 0:
        raise SystemExit("starting cash must be a non-negative finite number")
    source, request_id = _context(source, request_id)
    details = {"cash": cash, "make_default": bool(make_default)}
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    made_default = False
    with writing(conn):
        if _idempotent_action(
            conn, "account.create", name, source, request_id, details
        ):
            print(f"idempotent replay: account '{name}'")
            return False
        if conn.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
            raise SystemExit(f"'{name}' already exists")
        conn.execute(
            "INSERT INTO accounts(name,cash,deposits,created) VALUES(?,?,?,?)",
            (name, cash, cash, ts),
        )
        conn.execute(
            "INSERT INTO cashflow(account,ts,amount) VALUES(?,?,?)", (name, ts, cash)
        )
        conn.execute("INSERT INTO risk_settings(account) VALUES(?)", (name,))
        if (
            make_default
            and not conn.execute(
                "SELECT 1 FROM config WHERE key='default_account'"
            ).fetchone()
        ):
            conn.execute("INSERT INTO config VALUES('default_account',?)", (name,))
            made_default = True
        _audit_locked(conn, "account.create", name, source, request_id, details)
    return made_default


def rename_account(conn, old, new, source="cli", request_id=None):
    new = new.strip()
    if not new:
        raise SystemExit("new name required")
    source, request_id = _context(source, request_id)
    details = {"old": old, "new": new}
    with writing(conn):
        if _idempotent_action(conn, "account.rename", old, source, request_id, details):
            print(f"idempotent replay: renamed '{old}' -> '{new}'")
            return
        if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (old,)).fetchone():
            raise SystemExit(f"no account '{old}'")
        if conn.execute("SELECT 1 FROM accounts WHERE name=?", (new,)).fetchone():
            raise SystemExit(f"'{new}' already exists")
        for tbl, col in [
            ("accounts", "name"),
            ("positions", "account"),
            ("orders", "account"),
            ("cashflow", "account"),
            ("risk_settings", "account"),
            ("corporate_actions", "account"),
            ("corporate_sync", "account"),
            ("watchlists", "account"),
            ("option_instructions", "account"),
        ]:
            conn.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new, old))
        conn.execute(
            "UPDATE config SET value=? WHERE key='default_account' AND value=?",
            (new, old),
        )
        _audit_locked(conn, "account.rename", old, source, request_id, details)
    print(f"renamed '{old}' -> '{new}'")


def wipe_account(conn, name, reset_cash=None, source="cli", request_id=None):
    """reset_cash=None deletes the account; a number resets balances and clears history."""
    if reset_cash is not None and (not math.isfinite(reset_cash) or reset_cash < 0):
        raise SystemExit("reset cash must be a non-negative finite number")
    source, request_id = _context(source, request_id)
    action = "account.delete" if reset_cash is None else "account.reset"
    details = {"reset_cash": reset_cash}
    with writing(conn):
        if _idempotent_action(conn, action, name, source, request_id, details):
            print(f"idempotent replay: {action} {name}")
            return
        if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
            raise SystemExit(f"no account '{name}'")
        conn.execute("DELETE FROM positions WHERE account=?", (name,))
        conn.execute("DELETE FROM orders WHERE account=?", (name,))
        conn.execute("DELETE FROM corporate_actions WHERE account=?", (name,))
        conn.execute("DELETE FROM corporate_sync WHERE account=?", (name,))
        conn.execute("DELETE FROM option_instructions WHERE account=?", (name,))
        conn.execute(
            "DELETE FROM watchlist_symbols WHERE watchlist_id IN"
            " (SELECT id FROM watchlists WHERE account=?)",
            (name,),
        )
        conn.execute("DELETE FROM watchlists WHERE account=?", (name,))
        if reset_cash is None:
            conn.execute("DELETE FROM accounts WHERE name=?", (name,))
            conn.execute("DELETE FROM cashflow WHERE account=?", (name,))
            conn.execute(
                "DELETE FROM config WHERE key='default_account' AND value=?", (name,)
            )
            conn.execute("DELETE FROM risk_settings WHERE account=?", (name,))
            has_default = conn.execute(
                "SELECT 1 FROM config WHERE key='default_account'"
            ).fetchone()
            if not has_default:
                fallback = conn.execute(
                    "SELECT name FROM accounts ORDER BY name LIMIT 1"
                ).fetchone()
                if fallback:
                    conn.execute(
                        "INSERT INTO config(key,value) VALUES('default_account',?)",
                        (fallback[0],),
                    )
        else:
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE accounts SET cash=?, deposits=?, realized=0, created=? WHERE name=?",
                (reset_cash, reset_cash, ts, name),
            )
            conn.execute("DELETE FROM cashflow WHERE account=?", (name,))
            conn.execute(
                "INSERT INTO cashflow(account, ts, amount) VALUES(?,?,?)",
                (name, ts, reset_cash),
            )
        _audit_locked(conn, action, name, source, request_id, details)
    if reset_cash is None:
        print(f"deleted '{name}'")
    else:
        print(f"reset '{name}' to {reset_cash:,.2f}")


def settle_expired(conn, price_fn=live_price):
    # ponytail: settles the day after expiry at that day's spot intrinsic, not expiry-day close.
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT account, symbol FROM positions WHERE asset_class='option'"
    ).fetchall()
    for account, occ in rows:
        root, expiry, strike, cp = parse_occ(occ)
        if expiry >= today:
            continue
        spot = price_fn(root)  # network fetch outside the write lock
        intrinsic = max(0.0, spot - strike) if cp == "C" else max(0.0, strike - spot)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with writing(conn):
            # Another agent may have settled this option while its quote was loading.
            current = conn.execute(
                "SELECT qty FROM positions WHERE account=? AND symbol=? AND asset_class='option'",
                (account, occ),
            ).fetchone()
            if not current:
                continue
            instruction = conn.execute(
                "SELECT instruction FROM option_instructions"
                " WHERE account=? AND symbol=?",
                (account, occ),
            ).fetchone()
            if instruction and instruction[0] == "do_not_exercise":
                intrinsic = 0.0
            qty = current[0]
            side = "sell" if qty > 0 else "buy"
            _fill_locked(
                conn, account, occ, side, abs(qty), intrinsic, enforce_risk=False
            )
            oid = _insert_order_locked(
                conn,
                account,
                occ,
                side,
                abs(qty),
                None,
                "settled",
                intrinsic,
                ts,
                "engine",
                None,
            )
            _audit_locked(
                conn,
                "option.settle",
                account,
                "engine",
                details={"order_id": oid, "symbol": occ, "intrinsic": intrinsic},
            )
        print(f"settled {occ} at intrinsic {intrinsic:.2f}")


def _auction_ready(time_in_force, now=None):
    from zoneinfo import ZoneInfo

    eastern = (now or datetime.now(timezone.utc)).astimezone(
        ZoneInfo("America/New_York")
    )
    minute = eastern.hour * 60 + eastern.minute
    if time_in_force == "opg":
        return eastern.weekday() < 5 and 570 <= minute <= 580
    if time_in_force == "cls":
        return eastern.weekday() < 5 and 950 <= minute <= 970
    return True


def _pending_order_decision(order, price):
    """Return (should_fill, updates, description) for one current quote."""
    side, kind = order["side"], order["order_type"]
    updates = {}
    if kind == "market":
        ready = _auction_ready(order["time_in_force"])
        return ready, updates, "awaiting auction" if not ready else "market"
    if kind == "limit":
        crossed = (
            price <= order["limit_price"]
            if side == "buy"
            else price >= order["limit_price"]
        )
        return crossed, updates, f"limit {order['limit_price']:.2f}"
    if kind in ("stop", "stop_limit"):
        triggered = order["triggered"] or (
            price >= order["stop_price"]
            if side == "buy"
            else price <= order["stop_price"]
        )
        updates["triggered"] = int(triggered)
        if not triggered:
            return False, updates, f"stop {order['stop_price']:.2f}"
        if kind == "stop":
            return True, updates, f"stop {order['stop_price']:.2f} triggered"
        crossed = (
            price <= order["limit_price"]
            if side == "buy"
            else price >= order["limit_price"]
        )
        return (
            crossed,
            updates,
            f"stop-limit {order['stop_price']:.2f}/{order['limit_price']:.2f}",
        )
    old_hwm = order["hwm"] if order["hwm"] is not None else price
    hwm = min(old_hwm, price) if side == "buy" else max(old_hwm, price)
    stop = (
        hwm + order["trail_price"]
        if side == "buy" and order["trail_price"] is not None
        else hwm - order["trail_price"]
        if order["trail_price"] is not None
        else hwm * (1 + order["trail_percent"] / 100)
        if side == "buy"
        else hwm * (1 - order["trail_percent"] / 100)
    )
    updates.update({"hwm": hwm, "stop_price": stop})
    triggered = price >= stop if side == "buy" else price <= stop
    return triggered, updates, f"trailing stop {stop:.2f}"


def _linked_after_fill_locked(conn, order):
    held = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE parent_id=? AND status='held'",
        (order["id"],),
    ).fetchone()[0]
    if held:
        conn.execute(
            "UPDATE orders SET status='pending' WHERE parent_id=? AND status='held'",
            (order["id"],),
        )
        return
    if order["parent_id"] is not None or order["order_class"] == "oco":
        root = order["parent_id"] or order["id"]
        conn.execute(
            "UPDATE orders SET status='canceled' WHERE (id=? OR parent_id=?)"
            " AND id<>? AND status IN ('pending','held')",
            (root, root, order["id"]),
        )


def _linked_after_terminal_locked(conn, order):
    """Retire linked legs when their parent or one OCO member terminates."""
    if order["parent_id"] is None:
        conn.execute(
            "UPDATE orders SET status='canceled' WHERE parent_id=?"
            " AND status IN ('pending','held')",
            (order["id"],),
        )
    elif order["order_class"] == "oco":
        root = order["parent_id"]
        conn.execute(
            "UPDATE orders SET status='canceled' WHERE (id=? OR parent_id=?)"
            " AND id<>? AND status IN ('pending','held')",
            (root, root, order["id"]),
        )


def tick(conn, price_fn=live_price):
    """Advance pending limit, stop, trailing, linked, and auction order state."""
    settle_expired(conn, price_fn)
    columns = (
        "id,account,symbol,side,qty,limit_price,status,filled_price,ts,source,request_id,"
        "reject_reason,order_type,stop_price,trail_price,trail_percent,hwm,time_in_force,"
        "extended_hours,notional,client_order_id,replaced_by,parent_id,order_class,triggered"
    )
    pending = [
        _order_dict(row)
        for row in conn.execute(
            f"SELECT {columns} FROM orders WHERE status='pending' ORDER BY id"
        ).fetchall()
    ]
    for snapshot in pending:
        oid = snapshot["id"]
        if (
            snapshot["time_in_force"] == "day"
            and snapshot["ts"][:10] < datetime.now(timezone.utc).date().isoformat()
        ):
            with writing(conn):
                changed = conn.execute(
                    "UPDATE orders SET status='expired' WHERE id=? AND status='pending'",
                    (oid,),
                )
                if changed.rowcount:
                    _linked_after_terminal_locked(conn, snapshot)
            if changed.rowcount:
                print(f"expired #{oid} {snapshot['symbol']}")
            continue
        try:
            price = price_fn(snapshot["symbol"])
        except SystemExit as exc:
            print(f"#{oid} {snapshot['symbol']}: {exc}")
            continue
        should_fill, updates, description = _pending_order_decision(snapshot, price)
        with writing(conn):
            row = conn.execute(
                f"SELECT {columns} FROM orders WHERE id=? AND status='pending'", (oid,)
            ).fetchone()
            if not row:
                continue
            current = _order_dict(row)
            should_fill, updates, description = _pending_order_decision(current, price)
            if updates:
                conn.execute(
                    "UPDATE orders SET hwm=COALESCE(?,hwm),stop_price=COALESCE(?,stop_price),"
                    "triggered=COALESCE(?,triggered) WHERE id=? AND status='pending'",
                    (
                        updates.get("hwm"),
                        updates.get("stop_price"),
                        updates.get("triggered"),
                        oid,
                    ),
                )
            if not should_fill:
                print(f"#{oid} {current['symbol']}: price {price:.2f}, {description}")
                continue
            try:
                _fill_locked(
                    conn,
                    current["account"],
                    current["symbol"],
                    current["side"],
                    current["qty"],
                    price,
                )
            except SystemExit as exc:
                reason = str(exc)
                conn.execute(
                    "UPDATE orders SET status='rejected',reject_reason=? WHERE id=? AND status='pending'",
                    (reason, oid),
                )
                _linked_after_terminal_locked(conn, current)
                _audit_locked(
                    conn,
                    "order.reject",
                    current["account"],
                    "engine",
                    details={"order_id": oid, "reason": reason},
                )
                print(f"rejected #{oid}: {reason}")
                continue
            conn.execute(
                "UPDATE orders SET status='filled',filled_price=? WHERE id=? AND status='pending'",
                (price, oid),
            )
            _linked_after_fill_locked(conn, current)
            _audit_locked(
                conn,
                "order.fill",
                current["account"],
                "engine",
                details={"order_id": oid, "filled_price": price},
            )
        if should_fill:
            print(
                f"filled #{oid} {snapshot['side']} {snapshot['qty']:g} "
                f"{snapshot['symbol']} @ {price:.2f} ({description})"
            )
        else:
            print(f"#{oid} {snapshot['symbol']}: price {price:.2f}, {description}")
    if not pending:
        print("no pending orders")


def _fetch_corporate_actions(symbol, start, end):
    import yfinance as yf

    history = yf.Ticker(symbol).history(
        start=start, end=end, actions=True, auto_adjust=False
    )
    actions = []
    for stamp, row in history.iterrows():
        action_date = stamp.strftime("%Y-%m-%d")
        split = float(row.get("Stock Splits", 0) or 0)
        dividend = float(row.get("Dividends", 0) or 0)
        if split:
            actions.append((action_date, "split", split))
        if dividend:
            actions.append((action_date, "dividend", dividend))
    return actions


def _position_qty_before(conn, account, symbol, action_date):
    events = []
    for ts, side, qty in conn.execute(
        "SELECT ts,side,qty FROM orders WHERE account=? AND symbol=?"
        " AND status IN ('filled','settled','exercised') AND substr(ts,1,10)<?",
        (account, symbol, action_date),
    ):
        events.append((ts, 1, qty if side == "buy" else -qty))
    for date, ratio in conn.execute(
        "SELECT action_date,value FROM corporate_actions"
        " WHERE account=? AND symbol=? AND kind='split' AND action_date<=?",
        (account, symbol, action_date),
    ):
        events.append((date, 0, ratio))  # splits apply before same-day trades
    qty = 0.0
    for _stamp, kind, value in sorted(events):
        qty = qty * value if kind == 0 else qty + value
    return qty


def sync_corporate_actions(
    conn, account=None, actions_fn=_fetch_corporate_actions, source="cli"
):
    """Apply previously unseen stock/ETF dividends and splits exactly once."""
    source, _ = _context(source)
    params = (account,) if account else ()
    where = "AND p.account=?" if account else ""
    rows = conn.execute(
        "SELECT p.account,p.symbol FROM positions p"
        " WHERE p.asset_class='spot' " + where + " ORDER BY p.account,p.symbol",
        params,
    ).fetchall()
    if (
        account
        and not conn.execute(
            "SELECT 1 FROM accounts WHERE name=?", (account,)
        ).fetchone()
    ):
        raise SystemExit(f"no account '{account}'")
    if not rows:
        print("no eligible stock/ETF positions")
        return 0

    today = datetime.now(timezone.utc).date()
    applied = 0
    for name, symbol in rows:
        first = conn.execute(
            "SELECT MIN(substr(ts,1,10)) FROM orders WHERE account=? AND symbol=?"
            " AND status IN ('filled','settled','exercised')",
            (name, symbol),
        ).fetchone()[0]
        if not first:
            continue
        synced = conn.execute(
            "SELECT last_date FROM corporate_sync WHERE account=? AND symbol=?",
            (name, symbol),
        ).fetchone()
        start = (
            first
            if not synced
            else max(
                first,
                (
                    datetime.fromisoformat(synced[0]).date() - timedelta(days=7)
                ).isoformat(),
            )
        )
        try:
            actions = actions_fn(symbol, start, (today + timedelta(days=1)).isoformat())
        except Exception as exc:
            print(f"{symbol}: corporate-action refresh failed: {exc}")
            continue
        for action_date, kind, value in sorted(
            actions, key=lambda item: (item[0], 0 if item[1] == "split" else 1)
        ):
            if action_date < first or action_date > today.isoformat() or value <= 0:
                continue
            with writing(conn):
                if conn.execute(
                    "SELECT 1 FROM corporate_actions WHERE account=? AND symbol=?"
                    " AND action_date=? AND kind=?",
                    (name, symbol, action_date, kind),
                ).fetchone():
                    continue
                cash_effect = 0.0
                if kind == "split":
                    conn.execute(
                        "UPDATE positions SET qty=qty*?,avg_cost=avg_cost/?"
                        " WHERE account=? AND symbol=?",
                        (value, value, name, symbol),
                    )
                elif kind == "dividend":
                    qty = _position_qty_before(conn, name, symbol, action_date)
                    cash_effect = qty * value
                    conn.execute(
                        "UPDATE accounts SET cash=cash+?,realized=realized+? WHERE name=?",
                        (cash_effect, cash_effect, name),
                    )
                else:
                    continue
                conn.execute(
                    "INSERT INTO corporate_actions"
                    "(account,symbol,action_date,kind,value,cash_effect) VALUES(?,?,?,?,?,?)",
                    (name, symbol, action_date, kind, value, cash_effect),
                )
                _audit_locked(
                    conn,
                    f"corporate.{kind}",
                    name,
                    source,
                    details={
                        "symbol": symbol,
                        "date": action_date,
                        "value": value,
                        "cash_effect": cash_effect,
                    },
                )
                applied += 1
        with writing(conn):
            conn.execute(
                "INSERT OR REPLACE INTO corporate_sync(account,symbol,last_date)"
                " VALUES(?,?,?)",
                (name, symbol, today.isoformat()),
            )
    print(f"corporate actions applied: {applied}")
    return applied


def search_assets(query, limit=8):
    query = query.strip()
    if not query:
        raise SystemExit("search query required")
    limit = max(1, min(int(limit), 25))
    import yfinance as yf

    try:
        quotes = yf.Search(query, max_results=limit, news_count=0).quotes
    except Exception as exc:
        raise SystemExit(f"asset search failed: {exc}") from None
    return [
        {
            "symbol": item.get("symbol"),
            "name": item.get("shortname") or item.get("longname") or "",
            "type": item.get("quoteType") or item.get("typeDisp") or "unknown",
            "exchange": item.get("exchange") or item.get("exchDisp") or "",
        }
        for item in quotes[:limit]
        if item.get("symbol")
    ]


def validate_asset(symbol, price_fn=live_price):
    symbol = symbol.strip().upper()
    if not symbol:
        raise SystemExit("symbol required")
    price = price_fn(symbol)
    asset_class, multiplier, margin = classify(symbol)
    return {
        "valid": True,
        "symbol": symbol,
        "asset_class": asset_class,
        "price": price,
        "multiplier": multiplier,
        "initial_margin": margin,
    }


def _symbols(value):
    values = value.split(",") if isinstance(value, str) else value or []
    result = []
    for raw in values:
        symbol = str(raw).strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def create_watchlist(conn, account, name, symbols=None, source="cli", request_id=None):
    source, request_id = _context(source, request_id)
    name = (name or "").strip()
    items = _symbols(symbols)
    if not name:
        raise SystemExit("watchlist name required")
    if len(items) > 200:
        raise SystemExit("watchlist cannot exceed 200 symbols")
    details = {"name": name, "symbols": items}
    with writing(conn):
        if _idempotent_action(
            conn, "watchlist.create", account, source, request_id, details
        ):
            row = conn.execute(
                "SELECT id FROM watchlists WHERE account=? AND name=?", (account, name)
            ).fetchone()
            print(f"idempotent replay: watchlist {name}")
            return row[0]
        if not conn.execute(
            "SELECT 1 FROM accounts WHERE name=?", (account,)
        ).fetchone():
            raise SystemExit(f"no account '{account}'")
        try:
            cur = conn.execute(
                "INSERT INTO watchlists(account,name,created) VALUES(?,?,?)",
                (
                    account,
                    name,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
        except sqlite3.IntegrityError:
            raise SystemExit(f"watchlist '{name}' already exists") from None
        wid = cur.lastrowid
        conn.executemany(
            "INSERT INTO watchlist_symbols(watchlist_id,symbol,position) VALUES(?,?,?)",
            [(wid, symbol, index) for index, symbol in enumerate(items)],
        )
        _audit_locked(conn, "watchlist.create", account, source, request_id, details)
    print(f"created watchlist '{name}' ({len(items)} symbols)")
    return wid


def list_watchlists(conn, account):
    if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (account,)).fetchone():
        raise SystemExit(f"no account '{account}'")
    return [
        {"id": wid, "name": name, "symbols": count, "created": created}
        for wid, name, created, count in conn.execute(
            "SELECT w.id,w.name,w.created,COUNT(s.symbol) FROM watchlists w"
            " LEFT JOIN watchlist_symbols s ON s.watchlist_id=w.id"
            " WHERE w.account=? GROUP BY w.id ORDER BY w.name",
            (account,),
        )
    ]


def get_watchlist(conn, account, name_or_id):
    if str(name_or_id).isdigit():
        row = conn.execute(
            "SELECT id,name,created FROM watchlists WHERE account=? AND id=?",
            (account, int(name_or_id)),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id,name,created FROM watchlists WHERE account=? AND name=?",
            (account, str(name_or_id)),
        ).fetchone()
    if not row:
        raise SystemExit(f"no watchlist '{name_or_id}'")
    wid, name, created = row
    symbols = [
        symbol
        for (symbol,) in conn.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY position,symbol",
            (wid,),
        )
    ]
    return {
        "id": wid,
        "account": account,
        "name": name,
        "created": created,
        "symbols": symbols,
    }


def add_watchlist_symbol(
    conn, account, name_or_id, symbol, source="cli", request_id=None
):
    source, request_id = _context(source, request_id)
    symbol = symbol.strip().upper()
    if not symbol:
        raise SystemExit("symbol required")
    details = {"watchlist": str(name_or_id), "symbol": symbol}
    with writing(conn):
        watchlist = get_watchlist(conn, account, name_or_id)
        if _idempotent_action(
            conn, "watchlist.add", account, source, request_id, details
        ):
            print(f"idempotent replay: {symbol} in {watchlist['name']}")
            return
        if symbol not in watchlist["symbols"]:
            position = conn.execute(
                "SELECT COALESCE(MAX(position),-1)+1 FROM watchlist_symbols WHERE watchlist_id=?",
                (watchlist["id"],),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO watchlist_symbols(watchlist_id,symbol,position) VALUES(?,?,?)",
                (watchlist["id"], symbol, position),
            )
        _audit_locked(conn, "watchlist.add", account, source, request_id, details)
    print(f"added {symbol} to '{watchlist['name']}'")


def remove_watchlist_symbol(
    conn, account, name_or_id, symbol, source="cli", request_id=None
):
    source, request_id = _context(source, request_id)
    symbol = symbol.strip().upper()
    details = {"watchlist": str(name_or_id), "symbol": symbol}
    with writing(conn):
        watchlist = get_watchlist(conn, account, name_or_id)
        if _idempotent_action(
            conn, "watchlist.remove", account, source, request_id, details
        ):
            print(f"idempotent replay: removed {symbol}")
            return
        cur = conn.execute(
            "DELETE FROM watchlist_symbols WHERE watchlist_id=? AND symbol=?",
            (watchlist["id"], symbol),
        )
        if not cur.rowcount:
            raise SystemExit(f"{symbol} is not in '{watchlist['name']}'")
        _audit_locked(conn, "watchlist.remove", account, source, request_id, details)
    print(f"removed {symbol} from '{watchlist['name']}'")


def delete_watchlist(conn, account, name_or_id, source="cli", request_id=None):
    source, request_id = _context(source, request_id)
    details = {"watchlist": str(name_or_id)}
    with writing(conn):
        if _idempotent_action(
            conn, "watchlist.delete", account, source, request_id, details
        ):
            print(f"idempotent replay: deleted watchlist {name_or_id}")
            return
        watchlist = get_watchlist(conn, account, name_or_id)
        conn.execute(
            "DELETE FROM watchlist_symbols WHERE watchlist_id=?", (watchlist["id"],)
        )
        conn.execute("DELETE FROM watchlists WHERE id=?", (watchlist["id"],))
        _audit_locked(conn, "watchlist.delete", account, source, request_id, details)
    print(f"deleted watchlist '{watchlist['name']}'")


def watchlist_quotes(conn, account, name_or_id, price_fn=live_price):
    watchlist = get_watchlist(conn, account, name_or_id)
    quotes = []
    for symbol in watchlist["symbols"]:
        try:
            quotes.append({"symbol": symbol, "price": price_fn(symbol), "error": None})
        except SystemExit as exc:
            quotes.append({"symbol": symbol, "price": None, "error": str(exc)})
    return {**watchlist, "quotes": quotes}


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def market_history(
    symbol, kind="bars", start=None, end=None, timeframe="1Day", limit=100
):
    """Yahoo-backed aggregated bars and indicative quote/trade history."""
    symbol = symbol.strip().upper()
    kind = kind.lower()
    if kind not in ("bars", "quotes", "trades"):
        raise SystemExit("history kind must be bars, quotes, or trades")
    intervals = {
        "1min": "1m",
        "5min": "5m",
        "15min": "15m",
        "1hour": "1h",
        "1day": "1d",
        "1week": "1wk",
        "1month": "1mo",
    }
    interval = intervals.get(timeframe.lower())
    if not interval:
        raise SystemExit(f"unsupported timeframe '{timeframe}'")
    limit = max(1, min(int(limit), 5000))
    _quiet_yf()
    import yfinance as yf

    kwargs = {"interval": interval, "auto_adjust": False}
    if start:
        kwargs["start"] = start
    if end:
        kwargs["end"] = end
    if not start and not end:
        kwargs["period"] = "1mo" if interval in ("1d", "1wk", "1mo") else "5d"
    try:
        frame = yf.Ticker(symbol).history(**kwargs).tail(limit)
    except Exception as exc:
        raise SystemExit(f"market history failed: {exc}") from None
    rows = []
    for stamp, row in frame.iterrows():
        timestamp = stamp.isoformat()
        close = _number(row.get("Close"))
        if kind == "bars":
            rows.append(
                {
                    "timestamp": timestamp,
                    "open": _number(row.get("Open")),
                    "high": _number(row.get("High")),
                    "low": _number(row.get("Low")),
                    "close": close,
                    "volume": _number(row.get("Volume")),
                }
            )
        elif kind == "trades":
            rows.append(
                {
                    "timestamp": timestamp,
                    "price": close,
                    "size": _number(row.get("Volume")),
                    "aggregated": True,
                }
            )
        else:
            rows.append(
                {"timestamp": timestamp, "bid": close, "ask": close, "indicative": True}
            )
    return {"symbol": symbol, "kind": kind, "timeframe": timeframe, "data": rows}


def latest_quote(symbol):
    symbol = symbol.strip().upper()
    _quiet_yf()
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    try:
        info = ticker.info or {}
    except Exception:
        info = {}
    price = live_price(symbol)
    bid, ask = _number(info.get("bid")), _number(info.get("ask"))
    return {
        "symbol": symbol,
        "bid": bid or price,
        "ask": ask or price,
        "last": price,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "indicative": bid is None or ask is None,
    }


def latest_trade(symbol):
    quote = latest_quote(symbol)
    return {
        "symbol": quote["symbol"],
        "price": quote["last"],
        "timestamp": quote["timestamp"],
    }


def market_snapshot(symbol):
    history = market_history(symbol, "bars", timeframe="1Day", limit=2)["data"]
    quote = latest_quote(symbol)
    previous = history[-2]["close"] if len(history) > 1 else None
    change = quote["last"] - previous if previous else None
    return {
        "symbol": quote["symbol"],
        "quote": quote,
        "latest_bar": history[-1] if history else None,
        "previous_close": previous,
        "change": change,
    }


def market_news(symbol, limit=10):
    symbol = symbol.strip().upper()
    limit = max(1, min(int(limit), 50))
    _quiet_yf()
    import yfinance as yf

    try:
        items = yf.Ticker(symbol).news or []
    except Exception as exc:
        raise SystemExit(f"news unavailable: {exc}") from None
    result = []
    for item in items[:limit]:
        content = item.get("content", item)
        canonical = content.get("canonicalUrl") or {}
        url = canonical.get("url") if isinstance(canonical, dict) else canonical
        result.append(
            {
                "title": content.get("title"),
                "publisher": content.get("provider", {}).get("displayName")
                or content.get("publisher"),
                "published": content.get("pubDate")
                or content.get("providerPublishTime"),
                "url": url or content.get("link"),
            }
        )
    return result


def market_screener(name="most_actives", limit=20):
    name = name.strip().lower().replace("-", "_")
    if name not in ("most_actives", "day_gainers", "day_losers"):
        raise SystemExit("screener must be most-actives, movers, gainers, or losers")
    limit = max(1, min(int(limit), 100))
    _quiet_yf()
    import yfinance as yf

    try:
        data = yf.screen(name, count=limit)
        quotes = data.get("quotes", []) if isinstance(data, dict) else []
    except Exception as exc:
        raise SystemExit(f"screener unavailable: {exc}") from None
    return [
        {
            "symbol": row.get("symbol"),
            "name": row.get("shortName"),
            "price": _number(row.get("regularMarketPrice")),
            "change_percent": _number(row.get("regularMarketChangePercent")),
            "volume": _number(row.get("regularMarketVolume")),
        }
        for row in quotes[:limit]
    ]


def crypto_orderbook(symbol):
    quote = latest_quote(symbol)
    return {
        "symbol": quote["symbol"],
        "timestamp": quote["timestamp"],
        "bids": [{"price": quote["bid"], "size": None}],
        "asks": [{"price": quote["ask"], "size": None}],
        "indicative": True,
        "note": "Yahoo Finance exposes top-of-book indications, not exchange depth.",
    }


def trade_history_csv(conn, account, limit=5000):
    if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (account,)).fetchone():
        raise SystemExit(f"no account '{account}'")
    limit = max(1, min(int(limit), 10_000))
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "timestamp",
            "side",
            "quantity",
            "symbol",
            "order_type",
            "limit_price",
            "stop_price",
            "trail_price",
            "trail_percent",
            "time_in_force",
            "extended_hours",
            "notional",
            "status",
            "filled_price",
            "source",
            "request_id",
            "client_order_id",
            "parent_id",
            "order_class",
            "reject_reason",
        ]
    )
    writer.writerows(
        conn.execute(
            "SELECT id,ts,side,qty,symbol,order_type,limit_price,stop_price,trail_price,"
            "trail_percent,time_in_force,extended_hours,notional,status,filled_price,source,"
            "request_id,client_order_id,parent_id,order_class,reject_reason"
            " FROM orders WHERE account=? ORDER BY id LIMIT ?",
            (account, limit),
        )
    )
    return output.getvalue()


def audit_events(conn, account=None, limit=100, offset=0):
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    if account:
        return conn.execute(
            "SELECT id,ts,source,request_id,action,account,details FROM audit_log"
            " WHERE account=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (account, limit, offset),
        ).fetchall()
    return conn.execute(
        "SELECT id,ts,source,request_id,action,account,details FROM audit_log"
        " ORDER BY id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()


def account_activities(
    conn, account, activity_type=None, start=None, end=None, limit=100
):
    """Unified fills/orders, transfers, dividends, splits, and option events."""
    if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (account,)).fetchone():
        raise SystemExit(f"no account '{account}'")
    allowed = None
    if activity_type:
        allowed = {
            value.strip().lower() for value in activity_type.split(",") if value.strip()
        }
    limit = max(1, min(int(limit), 1000))
    events = []
    for oid, ts, symbol, side, qty, status, price, kind, order_class in conn.execute(
        "SELECT id,ts,symbol,side,qty,status,filled_price,order_type,order_class"
        " FROM orders WHERE account=?",
        (account,),
    ):
        event_type = "fill" if status in ("filled", "settled", "exercised") else "order"
        events.append(
            {
                "timestamp": ts,
                "type": event_type,
                "id": oid,
                "symbol": symbol,
                "side": side,
                "qty": qty,
                "status": status,
                "price": price,
                "order_type": kind,
                "order_class": order_class,
            }
        )
    for cid, ts, amount in conn.execute(
        "SELECT id,ts,amount FROM cashflow WHERE account=?", (account,)
    ):
        events.append(
            {"timestamp": ts, "type": "transfer", "id": cid, "amount": amount}
        )
    for cid, date, symbol, kind, value, cash in conn.execute(
        "SELECT id,action_date,symbol,kind,value,cash_effect FROM corporate_actions"
        " WHERE account=?",
        (account,),
    ):
        events.append(
            {
                "timestamp": f"{date}T00:00:00+00:00",
                "type": kind,
                "id": cid,
                "symbol": symbol,
                "value": value,
                "cash_effect": cash,
            }
        )
    for symbol, instruction, qty, ts in conn.execute(
        "SELECT symbol,instruction,qty,ts FROM option_instructions WHERE account=?",
        (account,),
    ):
        events.append(
            {"timestamp": ts, "type": instruction, "symbol": symbol, "qty": qty}
        )
    result = []
    for event in sorted(events, key=lambda item: item["timestamp"], reverse=True):
        if allowed and event["type"] not in allowed:
            continue
        if start and event["timestamp"][:10] < start:
            continue
        if end and event["timestamp"][:10] > end:
            continue
        result.append(event)
        if len(result) >= limit:
            break
    return result


def backup_database(conn, directory=None):
    directory = directory or os.path.expanduser("~/.papertrade_backups")
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    descriptor, path = tempfile.mkstemp(
        prefix=f"papertrade-{stamp}-", suffix=".db", dir=directory
    )
    os.close(descriptor)
    target = sqlite3.connect(path)
    failed = False
    try:
        conn.backup(target)
    except BaseException:
        failed = True
        raise
    finally:
        target.close()
        if failed:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
    return path


def healthcheck(conn):
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    return {
        "status": "ok" if integrity == "ok" else "degraded",
        "integrity": integrity,
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "expected_schema_version": SCHEMA_VERSION,
        "accounts": conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
        "positions": conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
        "pending_orders": conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending'"
        ).fetchone()[0],
        "held_orders": conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='held'"
        ).fetchone()[0],
        "watchlists": conn.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0],
        "database": DB,
    }


def pnl(conn, account, price_fn=live_price):
    row = conn.execute(
        "SELECT cash, deposits, realized FROM accounts WHERE name=?", (account,)
    ).fetchone()
    if not row:
        raise SystemExit(f"no account '{account}'")
    cash, deposits, realized = row
    positions = conn.execute(
        "SELECT symbol, qty, avg_cost, mult, asset_class, margin"
        " FROM positions WHERE account=?",
        (account,),
    ).fetchall()
    marks = batch_prices([p[0] for p in positions], price_fn=price_fn)
    equity, unreal = cash, 0.0
    print(f"{'symbol':<22}{'side':<6}{'qty':>7}{'avg':>11}{'last':>11}{'unreal':>13}")
    for symbol, qty, avg, mult, ac, margin in positions:
        price = marks[symbol]
        u = qty * mult * (price - avg)
        unreal += u
        equity += (u + margin) if ac == "future" else qty * mult * price
        side = "long" if qty > 0 else "short"
        print(
            f"{symbol:<22}{side:<6}{abs(qty):>7g}{avg:>11.2f}{price:>11.2f}{u:>13.2f}"
        )
    total = realized + unreal
    ret = total / deposits * 100 if deposits else 0.0
    print(f"cash {cash:,.2f}  equity {equity:,.2f}")
    print(
        f"realized {realized:+,.2f}  unreal {unreal:+,.2f}  total {total:+,.2f} ({ret:+.2f}%)"
    )


def _daily_closes(symbols, start, end):
    """{symbol: {YYYY-MM-DD: close}}. Options have no reliable history -> empty (marked flat)."""
    _quiet_yf()
    import yfinance as yf

    ordered = list(dict.fromkeys(symbols))
    exclusive_end = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime(
        "%Y-%m-%d"
    )

    def fetch(symbol):
        if OCC_RE.match(symbol):
            return symbol, {}
        try:
            history = yf.Ticker(symbol).history(
                start=start,
                end=exclusive_end,
                interval="1d",
                auto_adjust=True,
                actions=False,
            )["Close"]
            return symbol, {
                stamp.strftime("%Y-%m-%d"): float(value)
                for stamp, value in history.items()
            }
        except Exception:
            return symbol, {}

    if len(ordered) < 2:
        return dict(fetch(symbol) for symbol in ordered)
    # Network I/O bound, not CPU bound -- see batch_prices() below for why
    # this is sized to the task count instead of a small fixed pool.
    with ThreadPoolExecutor(max_workers=min(48, len(ordered))) as executor:
        return dict(executor.map(fetch, ordered))


def batch_prices(symbols, price_fn=None, ignore_errors=False):
    """Fetch unique live marks concurrently while preserving input order."""
    ordered = list(dict.fromkeys(symbols))
    price_fn = price_fn or live_price

    def fetch(symbol):
        try:
            return symbol, price_fn(symbol)
        except SystemExit:
            if ignore_errors:
                return symbol, None
            raise

    if len(ordered) < 2:
        return dict(fetch(symbol) for symbol in ordered)
    # Network I/O bound (waiting on Yahoo Finance responses), not CPU bound,
    # so one worker per symbol lets them all run concurrently instead of
    # queuing behind a small fixed pool — capped to avoid opening an
    # unreasonable number of connections for a very large portfolio.
    with ThreadPoolExecutor(max_workers=min(48, len(ordered))) as executor:
        return dict(executor.map(fetch, ordered))


def current_equity(conn, account, price_fn=live_price):
    """Live mark-to-market equity right now, using current prices."""
    rows = conn.execute(
        "SELECT a.cash,p.symbol,p.qty,p.avg_cost,p.mult,p.asset_class,p.margin "
        "FROM accounts a LEFT JOIN positions p ON p.account=a.name "
        "WHERE a.name=? ORDER BY p.symbol",
        (account,),
    ).fetchall()
    if not rows:
        raise SystemExit(f"no account '{account}'")
    cash = rows[0][0]
    positions = [row[1:] for row in rows if row[1] is not None]
    marks = batch_prices([position[0] for position in positions], price_fn=price_fn)
    eq = cash
    for sym, qty, avg, mult, ac, margin in positions:
        px = marks[sym]
        eq += (qty * mult * (px - avg) + margin) if ac == "future" else qty * mult * px
    return eq


def equity_curve(
    conn, account, closes_fn=_daily_closes, live=False, with_cashflows=False
):
    """Daily mark-to-market equity from portfolio start to today, replayed from the ledger.
    Returns [(date, equity)]. Reuses the exact fill math via _apply.
    live=True appends a final point marked at current prices (so a just-started portfolio
    still renders a chart from its starting equity to now)."""
    import datetime as _dt

    flows = conn.execute(
        "SELECT ts, amount FROM cashflow WHERE account=? ORDER BY ts", (account,)
    ).fetchall()
    trades = conn.execute(
        "SELECT ts, symbol, side, qty, filled_price FROM orders"
        " WHERE account=? AND status IN ('filled','settled','exercised')"
        " AND filled_price IS NOT NULL ORDER BY ts",
        (account,),
    ).fetchall()
    stamps = [r[0] for r in flows] + [r[0] for r in trades]
    if not stamps:
        return ([], flows) if with_cashflows else []
    start, end = min(stamps)[:10], datetime.now().strftime("%Y-%m-%d")
    closes = closes_fn(sorted({t[1] for t in trades}), start, end)
    events = sorted(
        [(f[0], "cash", f[1]) for f in flows]
        + [(t[0], "trade", t[1:]) for t in trades],
        key=lambda e: e[0],
    )
    state = {"cash": 0.0, "realized": 0.0, "pos": {}}
    last_close, curve, ei = {}, [], 0
    day, d_end = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
    while day <= d_end:
        ds = day.isoformat()
        while ei < len(events) and events[ei][0][:10] <= ds:
            _, kind, payload = events[ei]
            if kind == "cash":
                state["cash"] += payload
            else:
                sym, side, q, fp = payload
                try:
                    _apply(state, sym, side, q, fp)
                except SystemExit:
                    pass  # a same-day flow may not have landed yet in replay; skip
            ei += 1
        for sym, hd in closes.items():
            if ds in hd:
                last_close[sym] = hd[ds]
        eq = state["cash"]
        for sym, p in state["pos"].items():
            c = last_close.get(sym, p["avg"])
            eq += (
                (p["qty"] * p["mult"] * (c - p["avg"]) + p["margin"])
                if p["ac"] == "future"
                else p["qty"] * p["mult"] * c
            )
        curve.append((ds, eq))
        day += _dt.timedelta(days=1)
    if live:
        try:
            live_eq = current_equity(conn, account)
            if curve and curve[-1][0] == end:
                curve[-1] = (
                    end,
                    live_eq,
                )  # replace today's close-mark with the live mark
            else:
                curve.append((end, live_eq))
        except SystemExit:
            pass
    # guarantee at least 2 points so a fresh portfolio still charts a flat line
    if len(curve) == 1:
        curve = [curve[0], curve[0]]
    return (curve, flows) if with_cashflows else curve


def performance_metrics(curve, cashflows=None):
    """Cashflow-adjusted stats from the calendar-daily equity curve (rf=0)."""
    import numpy as np
    import datetime as _dt

    if len(curve) < 2:
        return {}
    eq = np.array([e for _, e in curve], float)
    flow_by_date = {}
    flow_items = cashflows.items() if hasattr(cashflows, "items") else cashflows or []
    for timestamp, amount in flow_items:
        day = str(timestamp)[:10]
        flow_by_date[day] = flow_by_date.get(day, 0.0) + float(amount)
    initial_date = curve[0][0][:10]
    returns = []
    for index in range(1, len(eq)):
        previous = eq[index - 1]
        if not previous:
            continue
        day = curve[index][0][:10]
        external_flow = 0.0 if day == initial_date else flow_by_date.get(day, 0.0)
        period_return = (eq[index] - external_flow) / previous - 1
        if np.isfinite(period_return):
            returns.append(period_return)
    rets = np.array(returns, dtype=float)
    days = max(
        1,
        (
            _dt.date.fromisoformat(curve[-1][0]) - _dt.date.fromisoformat(curve[0][0])
        ).days,
    )
    sd = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    dn = rets[rets < 0]
    dsd = float(dn.std(ddof=1)) if len(dn) > 1 else 0.0
    growth = np.cumprod(1 + rets) if len(rets) else np.array([1.0])
    total = float(growth[-1] - 1)
    unitized = np.concatenate(([1.0], growth))
    peak = np.maximum.accumulate(unitized)
    annual_factor = np.sqrt(365)
    return {
        "start": curve[0][0],
        "end": curve[-1][0],
        "days": days,
        "start_eq": float(eq[0]),
        "end_eq": float(eq[-1]),
        "total": total,
        "cagr": (float((1 + total) ** (365 / days) - 1) if 1 + total > 0 else -1.0),
        "vol": sd * annual_factor,
        "sharpe": float(rets.mean() / sd * annual_factor) if sd > 0 else 0.0,
        "sortino": float(rets.mean() / dsd * annual_factor) if dsd > 0 else 0.0,
        "mdd": float((unitized / peak - 1).min()),
        "best": float(rets.max()) if len(rets) else 0.0,
        "worst": float(rets.min()) if len(rets) else 0.0,
    }


def account_performance(conn, account, closes_fn=_daily_closes, live=True):
    """Return an equity curve and time-weighted metrics for one account."""
    curve, cashflows = equity_curve(
        conn,
        account,
        closes_fn=closes_fn,
        live=live,
        with_cashflows=True,
    )
    return curve, performance_metrics(curve, cashflows=cashflows)


def sparkline(values):
    bars = "▁▂▃▄▅▆▇█"
    v = list(values)
    if not v:
        return ""
    lo, hi = min(v), max(v)
    if hi == lo:
        return bars[3] * len(v)
    return "".join(bars[min(7, int((x - lo) / (hi - lo) * 7.999))] for x in v)


def show_perf(conn, account):
    curve, m = account_performance(conn, account, live=True)
    if not m:
        print(f"{account}: no activity yet")
        return
    print(f"{account}  {m['start']} → {m['end']}  ({m['days']}d)")
    print(sparkline([e for _, e in curve]))
    print(f"equity   {m['start_eq']:,.0f} → {m['end_eq']:,.0f}")
    print(f"return   {m['total'] * 100:+.2f}%   CAGR {m['cagr'] * 100:+.2f}%")
    print(
        f"sharpe   {m['sharpe']:.2f}   sortino {m['sortino']:.2f}   vol {m['vol'] * 100:.1f}%"
    )
    print(
        f"maxDD    {m['mdd'] * 100:.2f}%   best {m['best'] * 100:+.2f}%   worst {m['worst'] * 100:+.2f}%"
    )


def show_chain(root, expiry=None):
    import yfinance as yf

    tk = yf.Ticker(root.upper())
    exps = list(tk.options)
    if not exps:
        raise SystemExit(f"no listed options for {root.upper()}")
    if not expiry:
        print(f"{root.upper()} expiries: " + ", ".join(exps))
        return
    if expiry not in exps:
        raise SystemExit(f"no {expiry} expiry — available: {', '.join(exps[:12])}")
    ch = tk.option_chain(expiry)
    spot = live_price(root.upper())
    calls = ch.calls.set_index("strike")["lastPrice"]
    puts = ch.puts.set_index("strike")["lastPrice"]
    print(f"{root.upper()} {expiry}   spot {spot:.2f}   (strikes near the money)")
    print(f"{'strike':>10}{'call':>10}{'put':>10}   sample OCC")
    for k in sorted(set(calls.index) | set(puts.index)):
        if 0.85 * spot <= k <= 1.15 * spot:
            occ = build_occ(root, expiry, k, "C")
            print(
                f"{k:>10.2f}{calls.get(k, float('nan')):>10.2f}{puts.get(k, float('nan')):>10.2f}   {occ}"
            )


def resolve_account(conn, account):
    if account:
        return account
    row = conn.execute(
        "SELECT value FROM config WHERE key='default_account'"
    ).fetchone()
    if not row:
        raise SystemExit(
            "no account given and no default set — run: tradingcli use NAME"
        )
    return row[0]


CLI_COMMAND_TREE = {
    "account": ["new", "accounts", "use", "rename", "deposit", "withdraw", "risk"],
    "order": ["submit", "list", "get", "replace", "cancel", "cancel-all", "preview"],
    "position": ["list", "get", "close", "close-all"],
    "option": ["buy", "sell", "get", "exercise", "do-not-exercise", "mleg", "chain"],
    "watchlist": ["create", "list", "get", "add", "remove", "delete", "quotes"],
    "data": [
        "bars",
        "quotes",
        "trades",
        "latest-bar",
        "latest-quote",
        "latest-trade",
        "snapshot",
        "news",
        "most-actives",
        "movers",
        "crypto-orderbook",
        "forex",
    ],
    "operations": [
        "backtest",
        "calendar",
        "market",
        "activity",
        "audit",
        "actions",
        "tick",
        "doctor",
        "backup",
    ],
}


def _build_parser():
    p = argparse.ArgumentParser(
        prog="tradingcli",
        epilog="Global automation flags: --json --csv --quiet --schema --help-all",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("new", help="create account")
    c.add_argument("name")
    c.add_argument("--cash", type=float, default=100_000)
    sub.add_parser("accounts", help="list accounts")
    u = sub.add_parser("use", help="set default account")
    u.add_argument("name")
    for side in ("buy", "sell"):
        parser = sub.add_parser(side, help=f"simple market/limit {side}")
        parser.add_argument("symbol")
        parser.add_argument("qty", type=float)
        parser.add_argument("-a", "--account")
        parser.add_argument("--limit", type=float)

    order = sub.add_parser("order", help="full Alpaca-style order lifecycle")
    order_sub = order.add_subparsers(dest="order_cmd", required=True)
    submit = order_sub.add_parser("submit")
    submit.add_argument("symbol")
    submit.add_argument("--side", choices=["buy", "sell"], required=True)
    amount = submit.add_mutually_exclusive_group(required=True)
    amount.add_argument("--qty", type=float)
    amount.add_argument("--notional", type=float)
    submit.add_argument("--type", default="market")
    submit.add_argument("--limit-price", type=float)
    submit.add_argument("--stop-price", type=float)
    submit.add_argument("--trail-price", type=float)
    submit.add_argument("--trail-percent", type=float)
    submit.add_argument("--time-in-force", default="gtc", choices=sorted(TIME_IN_FORCE))
    submit.add_argument("--extended-hours", action="store_true")
    submit.add_argument(
        "--order-class", default="simple", choices=["simple", "bracket", "oco", "oto"]
    )
    submit.add_argument("--take-profit", type=float)
    submit.add_argument("--stop-loss", type=float)
    submit.add_argument("--stop-loss-limit", type=float)
    submit.add_argument("--client-order-id")
    submit.add_argument("--idempotency-key")
    submit.add_argument("--dry-run", action="store_true")
    submit.add_argument("-a", "--account")
    get = order_sub.add_parser("get")
    target = get.add_mutually_exclusive_group(required=True)
    target.add_argument("--order-id", type=int)
    target.add_argument("--client-order-id")
    get.add_argument("-a", "--account")
    listing = order_sub.add_parser("list")
    listing.add_argument("-a", "--account")
    listing.add_argument("--status", default="all")
    replace = order_sub.add_parser("replace")
    replace.add_argument("order_id", type=int)
    replace.add_argument("--qty", type=float)
    replace.add_argument("--limit-price", type=float)
    replace.add_argument("--stop-price", type=float)
    replace.add_argument("--trail", type=float)
    replace.add_argument("--time-in-force", choices=sorted(TIME_IN_FORCE))
    replace.add_argument("--client-order-id")
    replace.add_argument("--idempotency-key")
    cancel_one = order_sub.add_parser("cancel")
    cancel_one.add_argument("order_id", type=int)
    cancel_one.add_argument("--idempotency-key")
    cancel_all = order_sub.add_parser("cancel-all")
    cancel_scope = cancel_all.add_mutually_exclusive_group()
    cancel_scope.add_argument("-a", "--account")
    cancel_scope.add_argument(
        "--all-accounts", action="store_true", help="cancel across every account"
    )
    cancel_all.add_argument("--idempotency-key")

    position = sub.add_parser("position", help="position lookup and liquidation")
    position_sub = position.add_subparsers(dest="position_cmd", required=True)
    position_sub.add_parser("list").add_argument("-a", "--account")
    close_all_parser = position_sub.add_parser("close-all")
    close_all_parser.add_argument("-a", "--account")
    close_all_parser.add_argument("--idempotency-key")
    for command in ("get", "close"):
        parser = position_sub.add_parser(command)
        parser.add_argument("symbol")
        parser.add_argument("-a", "--account")
        if command == "close":
            size = parser.add_mutually_exclusive_group()
            size.add_argument("--qty", type=float)
            size.add_argument("--percent", type=float)
            parser.add_argument("--idempotency-key")

    option = sub.add_parser("option", help="trade and manage options")
    option_sub = option.add_subparsers(dest="osub", required=True)
    for side in ("buy", "sell"):
        parser = option_sub.add_parser(side)
        parser.add_argument("underlying")
        parser.add_argument("expiry", help="YYYY-MM-DD")
        parser.add_argument("strike", type=float)
        parser.add_argument("kind", choices=["C", "P", "c", "p"])
        parser.add_argument("qty", type=float)
        parser.add_argument("-a", "--account")
        parser.add_argument("--limit", type=float)
    option_sub.add_parser("get").add_argument("symbol")
    exercise = option_sub.add_parser("exercise")
    exercise.add_argument("symbol")
    exercise.add_argument("--qty", type=float)
    exercise.add_argument("-a", "--account")
    exercise.add_argument("--idempotency-key")
    dne = option_sub.add_parser("do-not-exercise")
    dne.add_argument("symbol")
    dne.add_argument("-a", "--account")
    dne.add_argument("--idempotency-key")
    mleg = option_sub.add_parser("mleg", help="JSON list of two-to-four option legs")
    mleg.add_argument("legs")
    mleg.add_argument("--limit-price", type=float)
    mleg.add_argument("--client-order-id")
    mleg.add_argument("--idempotency-key")
    mleg.add_argument("-a", "--account")

    watchlist = sub.add_parser("watchlist", help="persistent named watchlists")
    watch_sub = watchlist.add_subparsers(dest="watch_cmd", required=True)
    watch_create = watch_sub.add_parser("create")
    watch_create.add_argument("name")
    watch_create.add_argument("--symbols", default="")
    watch_create.add_argument("-a", "--account")
    watch_sub.add_parser("list").add_argument("-a", "--account")
    for command in ("get", "delete", "quotes"):
        parser = watch_sub.add_parser(command)
        parser.add_argument("watchlist")
        parser.add_argument("-a", "--account")
    for command in ("add", "remove"):
        parser = watch_sub.add_parser(command)
        parser.add_argument("watchlist")
        parser.add_argument("symbol")
        parser.add_argument("-a", "--account")

    data = sub.add_parser("data", help="market data and research")
    data_sub = data.add_subparsers(dest="data_cmd", required=True)
    for command in ("bars", "quotes", "trades"):
        parser = data_sub.add_parser(command)
        parser.add_argument("symbol")
        parser.add_argument("--start")
        parser.add_argument("--end")
        parser.add_argument("--timeframe", default="1Day")
        parser.add_argument("--limit", type=int, default=100)
    for command in (
        "latest-bar",
        "latest-quote",
        "latest-trade",
        "snapshot",
        "crypto-orderbook",
    ):
        data_sub.add_parser(command).add_argument("symbol")
    news = data_sub.add_parser("news")
    news.add_argument("symbol")
    news.add_argument("--limit", type=int, default=10)
    active = data_sub.add_parser("most-actives")
    active.add_argument("--limit", type=int, default=20)
    movers = data_sub.add_parser("movers")
    movers.add_argument("--limit", type=int, default=10)
    forex = data_sub.add_parser("forex")
    forex.add_argument("pair", help="for example USD/EUR")

    asset = sub.add_parser("asset", help="asset discovery")
    asset_sub = asset.add_subparsers(dest="asset_cmd", required=True)
    asset_sub.add_parser("list").add_argument("--limit", type=int, default=20)
    asset_sub.add_parser("get").add_argument("symbol")
    asset_search_parser = asset_sub.add_parser("search")
    asset_search_parser.add_argument("query")
    asset_search_parser.add_argument("--limit", type=int, default=8)

    chain = sub.add_parser("chain", help="list option expiries/strikes")
    chain.add_argument("underlying")
    chain.add_argument("expiry", nargs="?")
    find = sub.add_parser("find", help="search tradable symbols")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=8)
    sub.add_parser("validate").add_argument("symbol")
    sub.add_parser("quote").add_argument("symbol")
    sub.add_parser("market", help="NYSE status and next open/close")
    calendar = sub.add_parser("calendar")
    calendar.add_argument("--start")
    calendar.add_argument("--end")
    activity = sub.add_parser("activity")
    activity.add_argument("-a", "--account")
    activity.add_argument("--type")
    activity.add_argument("--start")
    activity.add_argument("--end")
    activity.add_argument("--limit", type=int, default=100)
    sub.add_parser("tick")
    for command in ("positions", "orders", "pnl", "perf"):
        sub.add_parser(command).add_argument("-a", "--account")
    backtest = sub.add_parser(
        "backtest", help="backtest the selected portfolio's current open positions"
    )
    backtest.add_argument("-a", "--account")
    backtest.add_argument("--start", help="inclusive YYYY-MM-DD")
    backtest.add_argument("--end", help="inclusive YYYY-MM-DD")
    backtest.add_argument("--lookback-days", type=int, default=1825)
    backtest.add_argument("--commission-bps", type=float, default=10.0)
    rename = sub.add_parser("rename")
    rename.add_argument("old")
    rename.add_argument("new")
    close = sub.add_parser("close")
    close.add_argument("symbol")
    close.add_argument("-a", "--account")
    close.add_argument("--qty", type=float)
    close.add_argument("--percent", type=float)
    preview = sub.add_parser("preview")
    preview.add_argument("side", choices=["buy", "sell"])
    preview.add_argument("symbol")
    preview.add_argument("qty", type=float)
    preview.add_argument("--price", type=float)
    preview.add_argument("-a", "--account")
    risk = sub.add_parser("risk")
    risk.add_argument("-a", "--account")
    risk.add_argument("--allow-short", action=argparse.BooleanOptionalAction)
    risk.add_argument("--allow-naked-options", action=argparse.BooleanOptionalAction)
    risk.add_argument("--max-leverage", type=float)
    risk.add_argument("--max-order", type=float)
    risk.add_argument("--clear-max-order", action="store_true")
    actions = sub.add_parser("actions")
    actions.add_argument("-a", "--account")
    audit = sub.add_parser("audit")
    audit.add_argument("-a", "--account")
    audit.add_argument("--limit", type=int, default=50)
    export = sub.add_parser("export")
    export.add_argument("-a", "--account")
    export.add_argument("--limit", type=int, default=5000)
    sub.add_parser("backup")
    sub.add_parser("doctor")
    sub.add_parser("dash")
    sub.add_parser("cancel").add_argument("order_id", type=int)
    remove = sub.add_parser("rm")
    remove.add_argument("name")
    remove.add_argument("--yes", action="store_true")
    reset = sub.add_parser("reset")
    reset.add_argument("name")
    reset.add_argument("--cash", type=float, default=100_000)
    for command in ("deposit", "withdraw"):
        parser = sub.add_parser(command)
        parser.add_argument("amount", type=float)
        parser.add_argument("-a", "--account")
    return p


def _emit_mode(mode, output):
    if mode == "quiet":
        return
    if mode == "json":
        try:
            parsed = json.loads(output)
        except json.JSONDecodeError:
            parsed = {"ok": True, "output": output.rstrip().splitlines()}
        print(json.dumps(parsed, indent=2))
        return
    writer = csv.writer(sys.stdout)
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        parsed = None
    rows = (
        parsed
        if isinstance(parsed, list)
        else [parsed]
        if isinstance(parsed, dict)
        else []
    )
    if rows and all(isinstance(row, dict) for row in rows):
        fields = list(dict.fromkeys(key for row in rows for key in row))
        writer.writerow(fields)
        writer.writerows(
            [
                json.dumps(row.get(field))
                if isinstance(row.get(field), (dict, list))
                else row.get(field)
                for field in fields
            ]
            for row in rows
        )
    else:
        writer.writerow(["output"])
        writer.writerows([[line] for line in output.rstrip().splitlines()])


def main(argv=None, _inner=False):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not _inner:
        if "--schema" in argv:
            print(json.dumps(CLI_COMMAND_TREE, indent=2))
            return
        argv = ["--help" if value == "--help-all" else value for value in argv]
        modes = [mode for mode in ("json", "csv", "quiet") if f"--{mode}" in argv]
        if len(modes) > 1:
            raise SystemExit("choose only one of --json, --csv, or --quiet")
        if modes:
            cleaned = [value for value in argv if value != f"--{modes[0]}"]
            buffer = io.StringIO()
            errors = io.StringIO()
            try:
                with (
                    contextlib.redirect_stdout(buffer),
                    contextlib.redirect_stderr(errors),
                ):
                    main(cleaned, _inner=True)
            except SystemExit as exc:
                message = errors.getvalue().strip() or str(exc)
                print(json.dumps({"ok": False, "error": message}), file=sys.stderr)
                raise SystemExit(1) from None
            _emit_mode(modes[0], buffer.getvalue())
            return
    if not argv:
        argv = ["dash"]
    args = _build_parser().parse_args(argv)
    _run_cli(args)


def _run_cli(args):
    if args.cmd == "dash":
        script = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "dashboard.py"
        )
        os.execv(sys.executable, [sys.executable, script])
    if args.cmd == "chain":
        show_chain(args.underlying, args.expiry)
        return
    if args.cmd in ("find", "validate", "quote", "market", "calendar", "data", "asset"):
        if args.cmd == "find":
            result = search_assets(args.query, args.limit)
        elif args.cmd == "validate":
            result = validate_asset(args.symbol)
        elif args.cmd == "quote":
            result = latest_quote(args.symbol)
        elif args.cmd == "market":
            result = market_clock()
        elif args.cmd == "calendar":
            result = market_calendar(args.start, args.end)
        elif args.cmd == "asset":
            if args.asset_cmd == "get":
                result = validate_asset(args.symbol)
            elif args.asset_cmd == "search":
                result = search_assets(args.query, args.limit)
            else:
                result = market_screener("most_actives", args.limit)
        elif args.data_cmd in ("bars", "quotes", "trades"):
            result = market_history(
                args.symbol,
                args.data_cmd,
                args.start,
                args.end,
                args.timeframe,
                args.limit,
            )
        elif args.data_cmd == "latest-bar":
            rows = market_history(args.symbol, "bars", timeframe="1Day", limit=1)[
                "data"
            ]
            result = rows[-1] if rows else None
        elif args.data_cmd == "latest-quote":
            result = latest_quote(args.symbol)
        elif args.data_cmd == "latest-trade":
            result = latest_trade(args.symbol)
        elif args.data_cmd == "snapshot":
            result = market_snapshot(args.symbol)
        elif args.data_cmd == "news":
            result = market_news(args.symbol, args.limit)
        elif args.data_cmd == "most-actives":
            result = market_screener("most_actives", args.limit)
        elif args.data_cmd == "movers":
            result = {
                "gainers": market_screener("day_gainers", args.limit),
                "losers": market_screener("day_losers", args.limit),
            }
        elif args.data_cmd == "crypto-orderbook":
            result = crypto_orderbook(args.symbol)
        else:
            pair = args.pair.upper().replace("/", "")
            if len(pair) != 6:
                raise SystemExit("forex pair must look like USD/EUR")
            result = latest_quote(f"{pair}=X")
        print(json.dumps(result, indent=2))
        return

    conn = db()
    try:
        if args.cmd == "new":
            made_default = create_account(conn, args.name, args.cash)
            print(
                f"created '{args.name}' with {args.cash:,.2f}"
                + (" (set as default)" if made_default else "")
            )
        elif args.cmd == "accounts":
            default = conn.execute(
                "SELECT value FROM config WHERE key='default_account'"
            ).fetchone()
            result = [
                {
                    "name": name,
                    "cash": cash,
                    "default": bool(default and name == default[0]),
                }
                for name, cash in conn.execute(
                    "SELECT name,cash FROM accounts ORDER BY name"
                )
            ]
            print(json.dumps(result, indent=2))
        elif args.cmd == "use":
            set_default(conn, args.name)
        elif args.cmd == "backtest":
            from portfolio_backtest import run_portfolio_backtest

            result = run_portfolio_backtest(
                conn,
                resolve_account(conn, args.account),
                start=args.start,
                end=args.end,
                lookback_days=args.lookback_days,
                commission=args.commission_bps / 10_000,
            )
            print(json.dumps(result, indent=2))
        elif args.cmd in ("buy", "sell"):
            place(
                conn,
                resolve_account(conn, args.account),
                args.symbol,
                args.cmd,
                args.qty,
                args.limit,
            )
        elif args.cmd == "order":
            if args.order_cmd == "submit":
                account = resolve_account(conn, args.account)
                tp = (
                    {"limit_price": args.take_profit}
                    if args.take_profit is not None
                    else None
                )
                sl = (
                    {"stop_price": args.stop_loss, "limit_price": args.stop_loss_limit}
                    if args.stop_loss is not None
                    else None
                )
                submit_order(
                    conn,
                    account,
                    args.symbol,
                    args.side,
                    args.qty,
                    args.notional,
                    args.type,
                    args.limit_price,
                    args.stop_price,
                    args.trail_price,
                    args.trail_percent,
                    args.time_in_force,
                    args.extended_hours,
                    args.client_order_id,
                    args.order_class,
                    tp,
                    sl,
                    dry_run=args.dry_run,
                    source="cli",
                    request_id=args.idempotency_key or args.client_order_id,
                )
            elif args.order_cmd == "get":
                account = (
                    resolve_account(conn, args.account)
                    if args.client_order_id
                    else args.account
                )
                print(
                    json.dumps(
                        get_order(conn, args.order_id, args.client_order_id, account),
                        indent=2,
                    )
                )
            elif args.order_cmd == "list":
                account = resolve_account(conn, args.account)
                columns = (
                    "id,account,symbol,side,qty,limit_price,status,filled_price,ts,source,request_id,"
                    "reject_reason,order_type,stop_price,trail_price,trail_percent,hwm,time_in_force,"
                    "extended_hours,notional,client_order_id,replaced_by,parent_id,order_class,triggered"
                )
                params = [account]
                where = "account=?"
                if args.status != "all":
                    where += " AND status=?"
                    params.append(args.status)
                rows = conn.execute(
                    f"SELECT {columns} FROM orders WHERE {where} ORDER BY id DESC",
                    params,
                ).fetchall()
                print(json.dumps([_order_dict(row) for row in rows], indent=2))
            elif args.order_cmd == "replace":
                replace_order(
                    conn,
                    args.order_id,
                    args.qty,
                    args.limit_price,
                    args.stop_price,
                    args.trail,
                    args.time_in_force,
                    args.client_order_id,
                    request_id=args.idempotency_key,
                )
            elif args.order_cmd == "cancel":
                cancel(conn, args.order_id, request_id=args.idempotency_key)
            else:
                account = (
                    None if args.all_accounts else resolve_account(conn, args.account)
                )
                cancel_all_orders(conn, account, request_id=args.idempotency_key)
        elif args.cmd == "position":
            account = resolve_account(conn, args.account)
            if args.position_cmd == "list":
                print(json.dumps(list_positions(conn, account), indent=2))
            elif args.position_cmd == "get":
                print(json.dumps(get_position(conn, account, args.symbol), indent=2))
            elif args.position_cmd == "close":
                close_position(
                    conn,
                    account,
                    args.symbol,
                    args.qty,
                    args.percent,
                    request_id=args.idempotency_key,
                )
            else:
                close_all_positions(conn, account, request_id=args.idempotency_key)
        elif args.cmd == "option":
            if args.osub == "get":
                print(json.dumps(option_contract_details(args.symbol), indent=2))
            else:
                account = resolve_account(conn, args.account)
                if args.osub in ("buy", "sell"):
                    symbol = build_occ(
                        args.underlying, args.expiry, args.strike, args.kind
                    )
                    place(conn, account, symbol, args.osub, args.qty, args.limit)
                elif args.osub == "exercise":
                    exercise_option(
                        conn,
                        account,
                        args.symbol,
                        args.qty,
                        request_id=args.idempotency_key,
                    )
                elif args.osub == "do-not-exercise":
                    do_not_exercise_option(
                        conn, account, args.symbol, request_id=args.idempotency_key
                    )
                else:
                    try:
                        legs = json.loads(args.legs)
                    except json.JSONDecodeError as exc:
                        raise SystemExit(f"invalid legs JSON: {exc}") from None
                    submit_option_multileg(
                        conn,
                        account,
                        legs,
                        args.limit_price,
                        request_id=args.idempotency_key or args.client_order_id,
                        client_order_id=args.client_order_id,
                    )
        elif args.cmd == "watchlist":
            account = resolve_account(conn, args.account)
            if args.watch_cmd == "create":
                create_watchlist(conn, account, args.name, args.symbols)
            elif args.watch_cmd == "list":
                print(json.dumps(list_watchlists(conn, account), indent=2))
            elif args.watch_cmd == "get":
                print(
                    json.dumps(get_watchlist(conn, account, args.watchlist), indent=2)
                )
            elif args.watch_cmd == "quotes":
                print(
                    json.dumps(
                        watchlist_quotes(conn, account, args.watchlist), indent=2
                    )
                )
            elif args.watch_cmd == "add":
                add_watchlist_symbol(conn, account, args.watchlist, args.symbol)
            elif args.watch_cmd == "remove":
                remove_watchlist_symbol(conn, account, args.watchlist, args.symbol)
            else:
                delete_watchlist(conn, account, args.watchlist)
        elif args.cmd == "rename":
            rename_account(conn, args.old, args.new)
        elif args.cmd == "close":
            close_position(
                conn,
                resolve_account(conn, args.account),
                args.symbol,
                args.qty,
                args.percent,
            )
        elif args.cmd == "preview":
            print(
                json.dumps(
                    preview_order(
                        conn,
                        resolve_account(conn, args.account),
                        args.symbol,
                        args.side,
                        args.qty,
                        args.price,
                    ),
                    indent=2,
                )
            )
        elif args.cmd == "risk":
            account = resolve_account(conn, args.account)
            changes = (
                args.allow_short,
                args.allow_naked_options,
                args.max_leverage,
                args.max_order,
            )
            if any(value is not None for value in changes) or args.clear_max_order:
                set_risk_limits(conn, account, *changes, args.clear_max_order)
            else:
                print(json.dumps(risk_limits(conn, account), indent=2))
        elif args.cmd == "activity":
            account = resolve_account(conn, args.account)
            print(
                json.dumps(
                    account_activities(
                        conn, account, args.type, args.start, args.end, args.limit
                    ),
                    indent=2,
                )
            )
        elif args.cmd == "actions":
            sync_corporate_actions(conn, args.account)
        elif args.cmd == "audit":
            print(
                json.dumps(
                    [
                        {
                            "id": row[0],
                            "timestamp": row[1],
                            "source": row[2],
                            "request_id": row[3],
                            "action": row[4],
                            "account": row[5],
                            "details": json.loads(row[6] or "{}"),
                        }
                        for row in audit_events(conn, args.account, args.limit)
                    ],
                    indent=2,
                )
            )
        elif args.cmd == "export":
            print(
                trade_history_csv(
                    conn, resolve_account(conn, args.account), args.limit
                ),
                end="",
            )
        elif args.cmd == "backup":
            print(backup_database(conn))
        elif args.cmd == "doctor":
            print(json.dumps(healthcheck(conn), indent=2))
        elif args.cmd == "cancel":
            cancel(conn, args.order_id)
        elif args.cmd == "rm":
            if (
                not args.yes
                and input(f"delete '{args.name}' and all its history? [y/N] ").lower()
                != "y"
            ):
                raise SystemExit("aborted")
            wipe_account(conn, args.name)
        elif args.cmd == "reset":
            wipe_account(conn, args.name, reset_cash=args.cash)
        elif args.cmd in ("deposit", "withdraw"):
            adjust_cash(
                conn,
                resolve_account(conn, args.account),
                args.amount if args.cmd == "deposit" else -args.amount,
            )
        elif args.cmd == "tick":
            tick(conn)
        elif args.cmd in ("positions", "orders", "pnl", "perf"):
            account = resolve_account(conn, args.account)
            if args.cmd == "positions":
                print(json.dumps(list_positions(conn, account), indent=2))
            elif args.cmd == "orders":
                for row in conn.execute(
                    "SELECT id,ts,side,qty,symbol,order_type,limit_price,stop_price,status,filled_price"
                    " FROM orders WHERE account=? ORDER BY id",
                    (account,),
                ):
                    oid, ts, side, qty, symbol, kind, limit, stop, status, filled = row
                    price = (
                        f"@{filled:.2f}"
                        if filled is not None
                        else f"lim {limit:.2f}"
                        if limit is not None
                        else f"stop {stop:.2f}"
                        if stop is not None
                        else ""
                    )
                    print(
                        f"#{oid} {ts} {side} {qty:g} {symbol} {kind} {price} [{status}]"
                    )
            elif args.cmd == "pnl":
                pnl(conn, account)
            else:
                show_perf(conn, account)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
