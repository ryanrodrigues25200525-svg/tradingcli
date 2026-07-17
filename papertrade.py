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
SCHEMA_VERSION = 2
DEFAULT_RISK = {
    "allow_short": True,
    "allow_naked_options": False,
    "max_gross_leverage": 2.0,
    "max_order_notional": None,
}

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
  reject_reason TEXT);
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
    if close:
        matches = account == intent["account"] and symbol == intent["symbol"]
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
):
    cur = conn.execute(
        "INSERT INTO orders(account,symbol,side,qty,limit_price,status,filled_price,ts,"
        "source,request_id,reject_reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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


def close_position(
    conn,
    account,
    symbol,
    price_fn=live_price,
    source="cli",
    request_id=None,
):
    """Flatten a position at market (market order opposite to current quantity)."""
    source, request_id = _context(source, request_id)
    symbol = symbol.upper()
    intent = {"account": account, "symbol": symbol}
    existing = _existing_order(conn, source, request_id, intent, close=True)
    if existing:
        _print_replayed_order(existing)
        return existing["id"]
    if not conn.execute(
        "SELECT 1 FROM positions WHERE account=? AND symbol=?", (account, symbol)
    ).fetchone():
        raise SystemExit(f"no open position in {symbol}")
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
        qty = row[0]
        side = "sell" if qty > 0 else "buy"
        _fill_locked(conn, account, symbol, side, abs(qty), price)
        oid = _insert_order_locked(
            conn,
            account,
            symbol,
            side,
            abs(qty),
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
            {**intent, "order_id": oid, "qty": abs(qty), "filled_price": price},
        )
    print(f"filled #{oid} {side} {abs(qty):g} {symbol} @ {price:.2f}")
    return oid


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
        account_row = conn.execute(
            "SELECT account FROM orders WHERE id=?", (oid,)
        ).fetchone()
        account = account_row[0] if account_row else None
        details = {"order_id": oid}
        if _idempotent_action(
            conn, "order.cancel", account, source, request_id, details
        ):
            print(f"idempotent replay: canceled #{oid}")
            return
        row = conn.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
        if not row:
            raise SystemExit(f"no order #{oid}")
        if row[0] != "pending":
            raise SystemExit(f"order #{oid} is {row[0]}, not pending")
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
        if reset_cash is None:
            conn.execute("DELETE FROM accounts WHERE name=?", (name,))
            conn.execute("DELETE FROM cashflow WHERE account=?", (name,))
            conn.execute(
                "DELETE FROM config WHERE key='default_account' AND value=?", (name,)
            )
            conn.execute("DELETE FROM risk_settings WHERE account=?", (name,))
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


def tick(conn, price_fn=live_price):
    settle_expired(conn, price_fn)
    pending = conn.execute(
        "SELECT id,account,symbol,side,qty,limit_price FROM orders"
        " WHERE status='pending'"
    ).fetchall()
    for oid, account, symbol, side, qty, limit in pending:
        try:
            price = price_fn(symbol)  # network fetch outside the write lock
        except SystemExit as exc:
            print(f"#{oid} {symbol}: {exc}")
            continue
        crossed = price <= limit if side == "buy" else price >= limit
        if crossed:
            with writing(conn):
                # A concurrent tick/cancel may have handled it while the price loaded.
                current = conn.execute(
                    "SELECT account,symbol,side,qty,limit_price FROM orders"
                    " WHERE id=? AND status='pending'",
                    (oid,),
                ).fetchone()
                if not current:
                    continue
                account, symbol, side, qty, limit = current
                crossed = price <= limit if side == "buy" else price >= limit
                if not crossed:
                    continue
                try:
                    _fill_locked(conn, account, symbol, side, qty, price)
                except SystemExit as exc:
                    reason = str(exc)
                    conn.execute(
                        "UPDATE orders SET status='rejected', reject_reason=?"
                        " WHERE id=? AND status='pending'",
                        (reason, oid),
                    )
                    _audit_locked(
                        conn,
                        "order.reject",
                        account,
                        "engine",
                        details={"order_id": oid, "reason": reason},
                    )
                    print(f"rejected #{oid}: {reason}")
                    continue
                conn.execute(
                    "UPDATE orders SET status='filled', filled_price=?"
                    " WHERE id=? AND status='pending'",
                    (price, oid),
                )
                _audit_locked(
                    conn,
                    "order.fill",
                    account,
                    "engine",
                    details={"order_id": oid, "filled_price": price},
                )
            print(
                f"filled #{oid} {side} {qty:g} {symbol} @ {price:.2f} (limit {limit:.2f})"
            )
        else:
            print(
                f"#{oid} {side} {qty:g} {symbol} limit {limit:.2f}: price {price:.2f}, no fill"
            )
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
        " AND status IN ('filled','settled') AND substr(ts,1,10)<?",
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
            " AND status IN ('filled','settled')",
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
            "limit_price",
            "status",
            "filled_price",
            "source",
            "request_id",
            "reject_reason",
        ]
    )
    writer.writerows(
        conn.execute(
            "SELECT id,ts,side,qty,symbol,limit_price,status,filled_price,source,"
            "request_id,reject_reason FROM orders WHERE account=? ORDER BY id LIMIT ?",
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


def backup_database(conn, directory=None):
    directory = directory or os.path.expanduser("~/.papertrade_backups")
    os.makedirs(directory, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(directory, f"papertrade-{stamp}.db")
    target = sqlite3.connect(path)
    try:
        conn.backup(target)
    finally:
        target.close()
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
    equity, unreal = cash, 0.0
    print(f"{'symbol':<22}{'side':<6}{'qty':>7}{'avg':>11}{'last':>11}{'unreal':>13}")
    for symbol, qty, avg, mult, ac, margin in positions:
        price = price_fn(symbol)
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
    import yfinance as yf

    out = {}
    for s in symbols:
        if OCC_RE.match(s):
            out[s] = {}
            continue
        try:
            h = yf.Ticker(s).history(start=start, interval="1d")["Close"]
            out[s] = {d.strftime("%Y-%m-%d"): float(v) for d, v in h.items()}
        except Exception:
            out[s] = {}
    return out


def current_equity(conn, account, price_fn=live_price):
    """Live mark-to-market equity right now, using current prices."""
    row = conn.execute("SELECT cash FROM accounts WHERE name=?", (account,)).fetchone()
    if not row:
        raise SystemExit(f"no account '{account}'")
    eq = row[0]
    for sym, qty, avg, mult, ac, margin in conn.execute(
        "SELECT symbol,qty,avg_cost,mult,asset_class,margin FROM positions WHERE account=?",
        (account,),
    ):
        px = price_fn(sym)
        eq += (qty * mult * (px - avg) + margin) if ac == "future" else qty * mult * px
    return eq


def equity_curve(conn, account, closes_fn=_daily_closes, live=False):
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
        " WHERE account=? AND status IN ('filled','settled')"
        " AND filled_price IS NOT NULL ORDER BY ts",
        (account,),
    ).fetchall()
    stamps = [r[0] for r in flows] + [r[0] for r in trades]
    if not stamps:
        return []
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
    return curve


def performance_metrics(curve):
    """Risk/return stats from a daily equity curve. rf assumed 0; annualized on 252 trading days."""
    import numpy as np
    import datetime as _dt

    if len(curve) < 2:
        return {}
    eq = np.array([e for _, e in curve], float)
    rets = np.diff(eq) / np.where(eq[:-1] == 0, np.nan, eq[:-1])
    rets = rets[np.isfinite(rets)]
    days = max(
        1,
        (
            _dt.date.fromisoformat(curve[-1][0]) - _dt.date.fromisoformat(curve[0][0])
        ).days,
    )
    sd = float(rets.std(ddof=1)) if len(rets) > 1 else 0.0
    dn = rets[rets < 0]
    dsd = float(dn.std(ddof=1)) if len(dn) > 1 else 0.0
    peak = np.maximum.accumulate(eq)
    return {
        "start": curve[0][0],
        "end": curve[-1][0],
        "days": days,
        "start_eq": float(eq[0]),
        "end_eq": float(eq[-1]),
        "total": float(eq[-1] / eq[0] - 1) if eq[0] else 0.0,
        "cagr": float((eq[-1] / eq[0]) ** (365 / days) - 1) if eq[0] > 0 else 0.0,
        "vol": sd * np.sqrt(252),
        "sharpe": float(rets.mean() / sd * np.sqrt(252)) if sd > 0 else 0.0,
        "sortino": float(rets.mean() / dsd * np.sqrt(252)) if dsd > 0 else 0.0,
        "mdd": float((eq / peak - 1).min()),
        "best": float(rets.max()) if len(rets) else 0.0,
        "worst": float(rets.min()) if len(rets) else 0.0,
    }


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
    curve = equity_curve(conn, account, live=True)
    m = performance_metrics(curve)
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


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["dash"]  # bare `tradingcli` opens the dashboard
    p = argparse.ArgumentParser(prog="tradingcli")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("new", help="create account")
    c.add_argument("name")
    c.add_argument("--cash", type=float, default=100_000)
    sub.add_parser("accounts", help="list accounts")
    u = sub.add_parser("use", help="set default account")
    u.add_argument("name")
    for side in ("buy", "sell"):
        s = sub.add_parser(side)
        s.add_argument("symbol")
        s.add_argument("qty", type=float)
        s.add_argument("-a", "--account")
        s.add_argument("--limit", type=float)
    o = sub.add_parser("option", help="trade an option")
    osub = o.add_subparsers(dest="osub", required=True)
    for oside in ("buy", "sell"):
        x = osub.add_parser(oside)
        x.add_argument("underlying")
        x.add_argument("expiry", help="YYYY-MM-DD")
        x.add_argument("strike", type=float)
        x.add_argument("kind", choices=["C", "P", "c", "p"])
        x.add_argument("qty", type=float)
        x.add_argument("-a", "--account")
        x.add_argument("--limit", type=float)
    ch = sub.add_parser("chain", help="list option expiries/strikes")
    ch.add_argument("underlying")
    ch.add_argument("expiry", nargs="?")
    find = sub.add_parser("find", help="search tradable symbols")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=8)
    val = sub.add_parser("validate", help="validate and quote a symbol")
    val.add_argument("symbol")
    sub.add_parser("market", help="NYSE status and next open/close")
    sub.add_parser("tick")
    for cmd in ("positions", "orders", "pnl", "perf"):
        sub.add_parser(cmd).add_argument("-a", "--account")
    rn = sub.add_parser("rename", help="rename a portfolio")
    rn.add_argument("old")
    rn.add_argument("new")
    cl = sub.add_parser("close", help="flatten a position at market")
    cl.add_argument("symbol")
    cl.add_argument("-a", "--account")
    pv = sub.add_parser("preview", help="dry-run an order with risk checks")
    pv.add_argument("side", choices=["buy", "sell"])
    pv.add_argument("symbol")
    pv.add_argument("qty", type=float)
    pv.add_argument("--price", type=float)
    pv.add_argument("-a", "--account")
    risk = sub.add_parser("risk", help="show or change account risk limits")
    risk.add_argument("-a", "--account")
    risk.add_argument("--allow-short", action=argparse.BooleanOptionalAction)
    risk.add_argument("--allow-naked-options", action=argparse.BooleanOptionalAction)
    risk.add_argument("--max-leverage", type=float)
    risk.add_argument("--max-order", type=float)
    risk.add_argument("--clear-max-order", action="store_true")
    acts = sub.add_parser("actions", help="sync stock dividends and splits")
    acts.add_argument("-a", "--account")
    audit = sub.add_parser("audit", help="show attributed mutation history")
    audit.add_argument("-a", "--account")
    audit.add_argument("--limit", type=int, default=50)
    export = sub.add_parser("export", help="export order history as CSV")
    export.add_argument("-a", "--account")
    export.add_argument("--limit", type=int, default=5000)
    sub.add_parser("backup", help="create an online SQLite backup")
    sub.add_parser("dash", help="live dashboard")
    sub.add_parser("cancel", help="cancel pending order").add_argument(
        "order_id", type=int
    )
    r = sub.add_parser("rm", help="delete account")
    r.add_argument("name")
    r.add_argument("--yes", action="store_true")
    r = sub.add_parser("reset", help="wipe trades, restore cash")
    r.add_argument("name")
    r.add_argument("--cash", type=float, default=100_000)
    for cmd in ("deposit", "withdraw"):
        d = sub.add_parser(cmd)
        d.add_argument("amount", type=float)
        d.add_argument("-a", "--account")
    a = p.parse_args(argv)

    if a.cmd == "dash":
        script = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "dashboard.py"
        )
        os.execv(sys.executable, [sys.executable, script])
    if a.cmd == "chain":
        show_chain(a.underlying, a.expiry)
        return
    if a.cmd == "find":
        print(json.dumps(search_assets(a.query, a.limit), indent=2))
        return
    if a.cmd == "market":
        print(json.dumps(market_clock(), indent=2))
        return
    if a.cmd == "validate":
        print(json.dumps(validate_asset(a.symbol), indent=2))
        return

    conn = db()
    with conn:
        if a.cmd == "new":
            made_default = create_account(conn, a.name, a.cash)
            suffix = " (set as default)" if made_default else ""
            print(f"created '{a.name}' with {a.cash:,.2f}{suffix}")
        elif a.cmd == "accounts":
            default = (
                resolve_account(conn, None)
                if conn.execute(
                    "SELECT 1 FROM config WHERE key='default_account'"
                ).fetchone()
                else None
            )
            for name, cash in conn.execute("SELECT name, cash FROM accounts"):
                mark = " *" if name == default else ""
                print(f"{name:<16}{cash:>14,.2f}{mark}")
        elif a.cmd == "use":
            set_default(conn, a.name)
        elif a.cmd in ("buy", "sell"):
            a.account = resolve_account(conn, a.account)
            place(conn, a.account, a.symbol, a.cmd, a.qty, a.limit)
        elif a.cmd == "option":
            account = resolve_account(conn, a.account)
            occ = build_occ(a.underlying, a.expiry, a.strike, a.kind)
            place(conn, account, occ, a.osub, a.qty, a.limit)
        elif a.cmd == "rename":
            rename_account(conn, a.old, a.new)
        elif a.cmd == "close":
            close_position(conn, resolve_account(conn, a.account), a.symbol)
        elif a.cmd == "preview":
            account = resolve_account(conn, a.account)
            print(
                json.dumps(
                    preview_order(conn, account, a.symbol, a.side, a.qty, a.price),
                    indent=2,
                )
            )
        elif a.cmd == "risk":
            account = resolve_account(conn, a.account)
            if (
                any(
                    value is not None
                    for value in (
                        a.allow_short,
                        a.allow_naked_options,
                        a.max_leverage,
                        a.max_order,
                    )
                )
                or a.clear_max_order
            ):
                set_risk_limits(
                    conn,
                    account,
                    a.allow_short,
                    a.allow_naked_options,
                    a.max_leverage,
                    a.max_order,
                    a.clear_max_order,
                )
            else:
                print(json.dumps(risk_limits(conn, account), indent=2))
        elif a.cmd == "actions":
            sync_corporate_actions(conn, a.account)
        elif a.cmd == "audit":
            for row in audit_events(conn, a.account, a.limit):
                oid, ts, source, request_id, action, account, details = row
                key = f" key={request_id}" if request_id else ""
                print(
                    f"#{oid} {ts} [{source}] {action} {account or '-'}{key} {details}"
                )
        elif a.cmd == "export":
            print(
                trade_history_csv(conn, resolve_account(conn, a.account), a.limit),
                end="",
            )
        elif a.cmd == "backup":
            print(backup_database(conn))
        elif a.cmd == "cancel":
            cancel(conn, a.order_id)
        elif a.cmd == "rm":
            if (
                not a.yes
                and input(f"delete '{a.name}' and all its history? [y/N] ").lower()
                != "y"
            ):
                raise SystemExit("aborted")
            wipe_account(conn, a.name)
        elif a.cmd == "reset":
            wipe_account(conn, a.name, reset_cash=a.cash)
        elif a.cmd in ("deposit", "withdraw"):
            account = resolve_account(conn, a.account)
            adjust_cash(conn, account, a.amount if a.cmd == "deposit" else -a.amount)
        elif a.cmd == "tick":
            tick(conn)
        elif a.cmd in ("positions", "orders", "pnl", "perf"):
            a.account = resolve_account(conn, a.account)
        if a.cmd == "positions":
            for symbol, qty, avg, ac in conn.execute(
                "SELECT symbol, qty, avg_cost, asset_class FROM positions WHERE account=?",
                (a.account,),
            ):
                side = "long" if qty > 0 else "short"
                print(f"{symbol:<22}{ac:<8}{side:<6}{abs(qty):>8g}{avg:>11.2f}")
        elif a.cmd == "orders":
            for row in conn.execute(
                "SELECT id,ts,side,qty,symbol,limit_price,status,filled_price"
                " FROM orders WHERE account=? ORDER BY id",
                (a.account,),
            ):
                oid, ts, side, qty, symbol, limit, status, fp = row
                px = f"@{fp:.2f}" if fp else (f"lim {limit:.2f}" if limit else "")
                print(f"#{oid} {ts} {side} {qty:g} {symbol} {px} [{status}]")
        elif a.cmd == "pnl":
            pnl(conn, a.account)
        elif a.cmd == "perf":
            show_perf(conn, a.account)
    conn.close()


if __name__ == "__main__":
    main()
