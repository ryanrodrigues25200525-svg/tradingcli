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
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from functools import lru_cache

import tradingcli_features as features

DB = os.environ.get("PAPERTRADE_DB", os.path.expanduser("~/.papertrade.db"))
VERSION = "0.5.0"
MARKET_DATA_TIMEOUT = max(
    1.0, float(os.environ.get("PAPERTRADE_MARKET_TIMEOUT", "15"))
)

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
SCHEMA_VERSION = 6
EXPECTED_DATA_GUARDS = 56
MONEY_QUANTUM = Decimal("0.01")
PRICE_QUANTUM = Decimal("0.000001")
QUANTITY_QUANTUM = Decimal("0.00000001")
DEFAULT_RISK = {
    "allow_short": True,
    "allow_naked_options": False,
    "max_gross_leverage": 2.0,
    "max_order_notional": None,
    "max_daily_loss": None,
    "max_drawdown": None,
    "max_symbol_exposure": None,
    "max_concentration": None,
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
SCHEMA += features.FEATURE_SCHEMA


def _fixed(value, quantum, label):
    """Return a finite float normalized with decimal half-even rounding."""
    try:
        decimal_value = Decimal(str(value))
        if not decimal_value.is_finite():
            raise InvalidOperation
        return float(decimal_value.quantize(quantum, rounding=ROUND_HALF_EVEN))
    except (InvalidOperation, TypeError, ValueError):
        raise SystemExit(f"{label} must be a finite number") from None


def _money(value):
    return _fixed(value, MONEY_QUANTUM, "money amount")


def _price(value):
    return _fixed(value, PRICE_QUANTUM, "price")


def _quantity(value):
    return _fixed(value, QUANTITY_QUANTUM, "quantity")


def _install_guard(conn, table, condition, message):
    """Install equivalent INSERT/UPDATE guards for one table invariant."""
    safe_name = re.sub(r"[^a-z0-9]+", "_", message.lower()).strip("_")[:40]
    for operation in ("INSERT", "UPDATE"):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS guard_{table}_{safe_name}_{operation.lower()}
            BEFORE {operation} ON {table}
            WHEN {condition}
            BEGIN
              SELECT RAISE(ABORT, '{message}');
            END
            """
        )


def _install_data_guards(conn):
    """Move critical domain and relationship validation into SQLite."""
    account_children = (
        "positions",
        "orders",
        "cashflow",
        "risk_settings",
        "corporate_actions",
        "corporate_sync",
        "watchlists",
        "option_instructions",
        "execution_settings",
        "journal_entries",
        "equity_peaks",
    )
    for table in account_children:
        _install_guard(
            conn,
            table,
            "NEW.account IS NULL OR NOT EXISTS"
            " (SELECT 1 FROM accounts WHERE name=NEW.account)",
            f"{table} account does not exist",
        )
    _install_guard(
        conn,
        "watchlist_symbols",
        "NEW.watchlist_id IS NULL OR NOT EXISTS"
        " (SELECT 1 FROM watchlists WHERE id=NEW.watchlist_id)",
        "watchlist does not exist",
    )
    account_references = " OR ".join(
        f"EXISTS(SELECT 1 FROM {table} WHERE account=OLD.name)"
        for table in account_children
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS guard_accounts_referenced_delete
        BEFORE DELETE ON accounts
        WHEN {account_references}
        BEGIN
          SELECT RAISE(ABORT, 'account is still referenced');
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS guard_accounts_referenced_name_update
        BEFORE UPDATE OF name ON accounts
        WHEN OLD.name<>NEW.name AND ({account_references})
        BEGIN
          SELECT RAISE(ABORT, 'account is still referenced');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS guard_watchlists_referenced_delete
        BEFORE DELETE ON watchlists
        WHEN EXISTS(SELECT 1 FROM watchlist_symbols WHERE watchlist_id=OLD.id)
        BEGIN
          SELECT RAISE(ABORT, 'watchlist is still referenced');
        END
        """
    )
    _install_guard(
        conn,
        "accounts",
        "NEW.name IS NULL OR trim(NEW.name)='' OR NEW.cash IS NULL"
        " OR NEW.cash<0 OR abs(NEW.cash)>1e15"
        " OR NEW.deposits IS NULL OR abs(NEW.deposits)>1e15"
        " OR NEW.realized IS NULL OR abs(NEW.realized)>1e15"
        " OR abs(NEW.cash*100-round(NEW.cash*100))>0.000001"
        " OR abs(NEW.deposits*100-round(NEW.deposits*100))>0.000001"
        " OR abs(NEW.realized*100-round(NEW.realized*100))>0.000001",
        "invalid account values",
    )
    _install_guard(
        conn,
        "positions",
        "NEW.symbol IS NULL OR trim(NEW.symbol)='' OR NEW.qty IS NULL OR NEW.qty=0"
        " OR abs(NEW.qty)>1e12 OR NEW.avg_cost IS NULL OR NEW.avg_cost<0"
        " OR abs(NEW.avg_cost)>1e15 OR NEW.mult IS NULL OR NEW.mult<=0"
        " OR NEW.margin IS NULL OR NEW.margin<0"
        " OR abs(NEW.qty*1e8-round(NEW.qty*1e8))>0.000001"
        " OR abs(NEW.avg_cost*1e6-round(NEW.avg_cost*1e6))>0.000001"
        " OR abs(NEW.mult*1e8-round(NEW.mult*1e8))>0.000001"
        " OR abs(NEW.margin*100-round(NEW.margin*100))>0.000001"
        " OR NEW.asset_class NOT IN ('spot','future','option')",
        "invalid position values",
    )
    _install_guard(
        conn,
        "orders",
        "NEW.symbol IS NULL OR trim(NEW.symbol)='' OR NEW.side NOT IN ('buy','sell')"
        " OR NEW.qty IS NULL OR NEW.qty<=0 OR abs(NEW.qty)>1e12"
        " OR NEW.status NOT IN"
        " ('pending','held','filled','canceled','rejected','replaced','expired',"
        "  'settled','exercised','partially_filled')"
        " OR NEW.order_type NOT IN"
        " ('market','limit','stop','stop_limit','trailing_stop')"
        " OR NEW.time_in_force NOT IN ('gtc','day','ioc','fok','opg','cls')"
        " OR NEW.order_class NOT IN ('simple','bracket','oco','oto','mleg')"
        " OR NEW.extended_hours NOT IN (0,1) OR NEW.triggered NOT IN (0,1)"
        " OR (NEW.limit_price IS NOT NULL AND NEW.limit_price<=0)"
        " OR (NEW.filled_price IS NOT NULL AND NEW.filled_price<0)"
        " OR (NEW.stop_price IS NOT NULL AND NEW.stop_price<=0)"
        " OR (NEW.trail_price IS NOT NULL AND NEW.trail_price<=0)"
        " OR (NEW.trail_percent IS NOT NULL AND NEW.trail_percent<=0)"
        " OR (NEW.notional IS NOT NULL AND NEW.notional<=0)"
        " OR NEW.commission<0 OR NEW.slippage<0"
        " OR (NEW.filled_qty IS NOT NULL AND NEW.filled_qty<=0)"
        " OR abs(NEW.qty*1e8-round(NEW.qty*1e8))>0.000001"
        " OR (NEW.limit_price IS NOT NULL"
        " AND abs(NEW.limit_price*1e6-round(NEW.limit_price*1e6))>0.000001)"
        " OR (NEW.filled_price IS NOT NULL"
        " AND abs(NEW.filled_price*1e6-round(NEW.filled_price*1e6))>0.000001)"
        " OR (NEW.stop_price IS NOT NULL"
        " AND abs(NEW.stop_price*1e6-round(NEW.stop_price*1e6))>0.000001)"
        " OR (NEW.trail_price IS NOT NULL"
        " AND abs(NEW.trail_price*1e6-round(NEW.trail_price*1e6))>0.000001)"
        " OR (NEW.trail_percent IS NOT NULL"
        " AND abs(NEW.trail_percent*1e8-round(NEW.trail_percent*1e8))>0.000001)"
        " OR (NEW.notional IS NOT NULL"
        " AND abs(NEW.notional*100-round(NEW.notional*100))>0.000001)"
        " OR abs(NEW.commission*100-round(NEW.commission*100))>0.000001"
        " OR abs(NEW.slippage*100-round(NEW.slippage*100))>0.000001"
        " OR (NEW.filled_qty IS NOT NULL"
        " AND abs(NEW.filled_qty*1e8-round(NEW.filled_qty*1e8))>0.000001)",
        "invalid order values",
    )
    _install_guard(
        conn,
        "cashflow",
        "NEW.amount IS NULL OR abs(NEW.amount)>1e15"
        " OR abs(NEW.amount*100-round(NEW.amount*100))>0.000001",
        "invalid cashflow values",
    )
    _install_guard(
        conn,
        "risk_settings",
        "NEW.allow_short NOT IN (0,1) OR NEW.allow_naked_options NOT IN (0,1)"
        " OR NEW.max_gross_leverage IS NULL OR NEW.max_gross_leverage<=0"
        " OR abs(NEW.max_gross_leverage*1e8"
        " -round(NEW.max_gross_leverage*1e8))>0.000001"
        " OR (NEW.max_order_notional IS NOT NULL AND NEW.max_order_notional<=0)"
        " OR (NEW.max_daily_loss IS NOT NULL AND NEW.max_daily_loss<=0)"
        " OR (NEW.max_symbol_exposure IS NOT NULL AND NEW.max_symbol_exposure<=0)"
        " OR (NEW.max_drawdown IS NOT NULL"
        " AND (NEW.max_drawdown<=0 OR NEW.max_drawdown>1))"
        " OR (NEW.max_concentration IS NOT NULL"
        " AND (NEW.max_concentration<=0 OR NEW.max_concentration>1))"
        " OR (NEW.max_order_notional IS NOT NULL"
        " AND abs(NEW.max_order_notional*100"
        " -round(NEW.max_order_notional*100))>0.000001)",
        "invalid risk settings",
    )
    _install_guard(
        conn,
        "corporate_actions",
        "NEW.kind NOT IN ('split','dividend') OR NEW.value IS NULL OR NEW.value<=0"
        " OR NEW.cash_effect IS NULL OR abs(NEW.cash_effect)>1e15"
        " OR abs(NEW.value*1e8-round(NEW.value*1e8))>0.000001"
        " OR abs(NEW.cash_effect*100-round(NEW.cash_effect*100))>0.000001",
        "invalid corporate action",
    )
    _install_guard(
        conn,
        "corporate_sync",
        "NEW.symbol IS NULL OR trim(NEW.symbol)=''"
        " OR NEW.last_date IS NULL OR trim(NEW.last_date)=''",
        "invalid corporate sync",
    )
    _install_guard(
        conn,
        "watchlists",
        "NEW.name IS NULL OR trim(NEW.name)=''"
        " OR NEW.created IS NULL OR trim(NEW.created)=''",
        "invalid watchlist",
    )
    _install_guard(
        conn,
        "watchlist_symbols",
        "NEW.symbol IS NULL OR trim(NEW.symbol)='' OR NEW.position<0",
        "invalid watchlist symbol",
    )
    _install_guard(
        conn,
        "option_instructions",
        "NEW.symbol IS NULL OR trim(NEW.symbol)=''"
        " OR NEW.instruction NOT IN ('exercise','do_not_exercise')"
        " OR (NEW.qty IS NOT NULL AND NEW.qty<=0)"
        " OR (NEW.qty IS NOT NULL"
        " AND abs(NEW.qty*1e8-round(NEW.qty*1e8))>0.000001)",
        "invalid option instruction",
    )


def _database_invariants(conn):
    """Count logical violations that SQLite's page check cannot detect."""
    orphan_checks = {
        table: conn.execute(
            f"SELECT COUNT(*) FROM {table} child"
            " LEFT JOIN accounts parent ON parent.name=child.account"
            " WHERE parent.name IS NULL"
        ).fetchone()[0]
        for table in (
            "positions",
            "orders",
            "cashflow",
            "risk_settings",
            "corporate_actions",
            "corporate_sync",
            "watchlists",
            "option_instructions",
            "execution_settings",
            "journal_entries",
            "equity_peaks",
        )
    }
    orphan_checks["watchlist_symbols"] = conn.execute(
        "SELECT COUNT(*) FROM watchlist_symbols child"
        " LEFT JOIN watchlists parent ON parent.id=child.watchlist_id"
        " WHERE parent.id IS NULL"
    ).fetchone()[0]
    domain_violations = {
        "accounts": conn.execute(
            "SELECT COUNT(*) FROM accounts WHERE name IS NULL OR trim(name)=''"
            " OR cash IS NULL OR cash<0 OR abs(cash)>1e15"
            " OR deposits IS NULL OR abs(deposits)>1e15"
            " OR realized IS NULL OR abs(realized)>1e15"
        ).fetchone()[0],
        "positions": conn.execute(
            "SELECT COUNT(*) FROM positions WHERE symbol IS NULL OR trim(symbol)=''"
            " OR qty IS NULL OR qty=0 OR abs(qty)>1e12"
            " OR avg_cost IS NULL OR avg_cost<0 OR mult IS NULL OR mult<=0"
            " OR margin IS NULL OR margin<0"
            " OR asset_class NOT IN ('spot','future','option')"
        ).fetchone()[0],
        "orders": conn.execute(
            "SELECT COUNT(*) FROM orders WHERE symbol IS NULL OR trim(symbol)=''"
            " OR side NOT IN ('buy','sell') OR qty IS NULL OR qty<=0"
            " OR status NOT IN"
            " ('pending','held','filled','canceled','rejected','replaced','expired',"
            "  'settled','exercised','partially_filled')"
            " OR order_type NOT IN ('market','limit','stop','stop_limit','trailing_stop')"
            " OR time_in_force NOT IN ('gtc','day','ioc','fok','opg','cls')"
            " OR order_class NOT IN ('simple','bracket','oco','oto','mleg')"
            " OR extended_hours NOT IN (0,1) OR triggered NOT IN (0,1)"
            " OR (limit_price IS NOT NULL AND limit_price<=0)"
            " OR (filled_price IS NOT NULL AND filled_price<0)"
            " OR (stop_price IS NOT NULL AND stop_price<=0)"
            " OR (trail_price IS NOT NULL AND trail_price<=0)"
            " OR (trail_percent IS NOT NULL AND trail_percent<=0)"
            " OR (notional IS NOT NULL AND notional<=0)"
            " OR commission<0 OR slippage<0"
            " OR (filled_qty IS NOT NULL AND filled_qty<=0)"
        ).fetchone()[0],
        "cashflow": conn.execute(
            "SELECT COUNT(*) FROM cashflow"
            " WHERE amount IS NULL OR abs(amount)>1e15"
        ).fetchone()[0],
        "risk_settings": conn.execute(
            "SELECT COUNT(*) FROM risk_settings"
            " WHERE allow_short NOT IN (0,1) OR allow_naked_options NOT IN (0,1)"
            " OR max_gross_leverage IS NULL OR max_gross_leverage<=0"
            " OR (max_order_notional IS NOT NULL AND max_order_notional<=0)"
            " OR (max_daily_loss IS NOT NULL AND max_daily_loss<=0)"
            " OR (max_symbol_exposure IS NOT NULL AND max_symbol_exposure<=0)"
            " OR (max_drawdown IS NOT NULL"
            " AND (max_drawdown<=0 OR max_drawdown>1))"
            " OR (max_concentration IS NOT NULL"
            " AND (max_concentration<=0 OR max_concentration>1))"
        ).fetchone()[0],
        "corporate_actions": conn.execute(
            "SELECT COUNT(*) FROM corporate_actions"
            " WHERE symbol IS NULL OR trim(symbol)=''"
            " OR kind NOT IN ('split','dividend') OR value IS NULL OR value<=0"
            " OR cash_effect IS NULL OR abs(cash_effect)>1e15"
        ).fetchone()[0],
        "corporate_sync": conn.execute(
            "SELECT COUNT(*) FROM corporate_sync"
            " WHERE symbol IS NULL OR trim(symbol)=''"
            " OR last_date IS NULL OR trim(last_date)=''"
        ).fetchone()[0],
        "watchlists": conn.execute(
            "SELECT COUNT(*) FROM watchlists"
            " WHERE name IS NULL OR trim(name)=''"
            " OR created IS NULL OR trim(created)=''"
        ).fetchone()[0],
        "watchlist_symbols": conn.execute(
            "SELECT COUNT(*) FROM watchlist_symbols"
            " WHERE symbol IS NULL OR trim(symbol)='' OR position<0"
        ).fetchone()[0],
        "option_instructions": conn.execute(
            "SELECT COUNT(*) FROM option_instructions"
            " WHERE symbol IS NULL OR trim(symbol)=''"
            " OR instruction NOT IN ('exercise','do_not_exercise')"
            " OR (qty IS NOT NULL AND qty<=0)"
        ).fetchone()[0],
    }
    precision_violations = {
        "accounts": conn.execute(
            "SELECT COUNT(*) FROM accounts"
            " WHERE abs(cash*100-round(cash*100))>0.000001"
            " OR abs(deposits*100-round(deposits*100))>0.000001"
            " OR abs(realized*100-round(realized*100))>0.000001"
        ).fetchone()[0],
        "cashflow": conn.execute(
            "SELECT COUNT(*) FROM cashflow"
            " WHERE abs(amount*100-round(amount*100))>0.000001"
        ).fetchone()[0],
        "positions": conn.execute(
            "SELECT COUNT(*) FROM positions"
            " WHERE abs(qty*1e8-round(qty*1e8))>0.000001"
            " OR abs(avg_cost*1e6-round(avg_cost*1e6))>0.000001"
            " OR abs(mult*1e8-round(mult*1e8))>0.000001"
            " OR abs(margin*100-round(margin*100))>0.000001"
        ).fetchone()[0],
        "orders": conn.execute(
            "SELECT COUNT(*) FROM orders"
            " WHERE abs(qty*1e8-round(qty*1e8))>0.000001"
            " OR (limit_price IS NOT NULL"
            " AND abs(limit_price*1e6-round(limit_price*1e6))>0.000001)"
            " OR (filled_price IS NOT NULL"
            " AND abs(filled_price*1e6-round(filled_price*1e6))>0.000001)"
            " OR (stop_price IS NOT NULL"
            " AND abs(stop_price*1e6-round(stop_price*1e6))>0.000001)"
            " OR (trail_price IS NOT NULL"
            " AND abs(trail_price*1e6-round(trail_price*1e6))>0.000001)"
            " OR (trail_percent IS NOT NULL"
            " AND abs(trail_percent*1e8-round(trail_percent*1e8))>0.000001)"
            " OR (notional IS NOT NULL"
            " AND abs(notional*100-round(notional*100))>0.000001)"
            " OR abs(commission*100-round(commission*100))>0.000001"
            " OR abs(slippage*100-round(slippage*100))>0.000001"
            " OR (filled_qty IS NOT NULL"
            " AND abs(filled_qty*1e8-round(filled_qty*1e8))>0.000001)"
        ).fetchone()[0],
        "risk_settings": conn.execute(
            "SELECT COUNT(*) FROM risk_settings"
            " WHERE abs(max_gross_leverage*1e8"
            " -round(max_gross_leverage*1e8))>0.000001"
            " OR (max_order_notional IS NOT NULL"
            " AND abs(max_order_notional*100-round(max_order_notional*100))>0.000001)"
            " OR (max_daily_loss IS NOT NULL"
            " AND abs(max_daily_loss*100-round(max_daily_loss*100))>0.000001)"
            " OR (max_symbol_exposure IS NOT NULL"
            " AND abs(max_symbol_exposure*100"
            " -round(max_symbol_exposure*100))>0.000001)"
            " OR (max_drawdown IS NOT NULL"
            " AND abs(max_drawdown*1e8-round(max_drawdown*1e8))>0.000001)"
            " OR (max_concentration IS NOT NULL"
            " AND abs(max_concentration*1e8-round(max_concentration*1e8))>0.000001)"
        ).fetchone()[0],
        "corporate_actions": conn.execute(
            "SELECT COUNT(*) FROM corporate_actions"
            " WHERE abs(value*1e8-round(value*1e8))>0.000001"
            " OR abs(cash_effect*100-round(cash_effect*100))>0.000001"
        ).fetchone()[0],
        "option_instructions": conn.execute(
            "SELECT COUNT(*) FROM option_instructions"
            " WHERE qty IS NOT NULL"
            " AND abs(qty*1e8-round(qty*1e8))>0.000001"
        ).fetchone()[0],
    }
    return {
        "orphans": orphan_checks,
        "domain": domain_violations,
        "precision": precision_violations,
        "total": sum(orphan_checks.values())
        + sum(domain_violations.values())
        + sum(precision_violations.values()),
    }


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
    features.prepare_migration(conn)
    _normalize_storage(conn)
    features.finalize_migration(conn)
    invariants = _database_invariants(conn)
    if invariants["total"]:
        raise RuntimeError(
            f"database migration blocked by {invariants['total']} logical violation(s)"
        )
    _install_data_guards(conn)
    features.install_guards(conn)


def _normalize_storage(conn):
    """Normalize persisted numeric values to their documented fixed precision."""
    specifications = (
        (
            "accounts",
            ("name",),
            (("cash", _money), ("deposits", _money), ("realized", _money)),
        ),
        (
            "positions",
            ("account", "symbol"),
            (
                ("qty", _quantity),
                ("avg_cost", _price),
                ("mult", _quantity),
                ("margin", _money),
            ),
        ),
        (
            "orders",
            ("id",),
            (
                ("qty", _quantity),
                ("limit_price", _price),
                ("filled_price", _price),
                ("stop_price", _price),
                ("trail_price", _price),
                ("trail_percent", _quantity),
                ("hwm", _price),
                ("notional", _money),
            ),
        ),
        ("cashflow", ("id",), (("amount", _money),)),
        (
            "risk_settings",
            ("account",),
            (
                ("max_gross_leverage", _quantity),
                ("max_order_notional", _money),
                ("max_daily_loss", _money),
                ("max_drawdown", _quantity),
                ("max_symbol_exposure", _money),
                ("max_concentration", _quantity),
            ),
        ),
        (
            "corporate_actions",
            ("id",),
            (("value", _quantity), ("cash_effect", _money)),
        ),
        (
            "option_instructions",
            ("account", "symbol"),
            (("qty", _quantity),),
        ),
    )
    try:
        for table, keys, fields in specifications:
            names = keys + tuple(name for name, _ in fields)
            rows = conn.execute(f"SELECT {','.join(names)} FROM {table}").fetchall()
            assignments = ",".join(f"{name}=?" for name, _ in fields)
            predicate = " AND ".join(f"{key}=?" for key in keys)
            normalized = []
            for row in rows:
                values = row[len(keys) :]
                converted = [
                    converter(value) if value is not None else None
                    for value, (_, converter) in zip(values, fields)
                ]
                normalized.append((*converted, *row[: len(keys)]))
            if normalized:
                conn.executemany(
                    f"UPDATE {table} SET {assignments} WHERE {predicate}",
                    normalized,
                )
    except SystemExit as exc:
        raise RuntimeError(f"database contains an invalid numeric value: {exc}") from exc


def _database_file_path():
    """Return the concrete SQLite path, or None for memory/URI databases."""
    if DB == ":memory:" or DB.startswith("file:"):
        return None
    return os.path.abspath(os.path.expanduser(DB))


def _secure_database_files(path):
    """Ensure portfolio state and SQLite sidecars are private to the owner."""
    if path is None:
        return
    for candidate in (path, f"{path}-wal", f"{path}-shm"):
        try:
            # chmod dirties filesystem metadata. Most calls are already
            # private, so avoid six redundant chmod syscalls per command.
            if os.stat(candidate).st_mode & 0o777 != 0o600:
                os.chmod(candidate, 0o600)
        except FileNotFoundError:
            continue


def _migration_backup(conn, path, from_version):
    """Create a private, consistent backup before changing an existing schema."""
    import tempfile

    directory = f"{path}.migrations"
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    descriptor, backup_path = tempfile.mkstemp(
        prefix=f"pre-v{from_version}-to-v{SCHEMA_VERSION}-{stamp}-",
        suffix=".db",
        dir=directory,
    )
    os.close(descriptor)
    os.chmod(backup_path, 0o600)
    target = sqlite3.connect(backup_path)
    try:
        conn.backup(target)
    finally:
        target.close()
    return backup_path


def db():
    # WAL + busy_timeout + autocommit so multiple agents (Codex, Claude Code, Hermes) share the DB.
    # Mutations must run inside writing() so BEGIN IMMEDIATE serializes read-modify-write.
    path = _database_file_path()
    if path is not None:
        # sqlite3.connect() otherwise creates files from the process umask,
        # commonly exposing portfolio state as world-readable mode 0644.
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        _secure_database_files(path)
    conn = sqlite3.connect(DB, timeout=10, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _secure_database_files(path)
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{current_version} is newer than supported"
                f" v{SCHEMA_VERSION}; upgrade tradingcli"
            )
        had_schema = bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
            ).fetchone()
        )
        backup_path = None
        if current_version < SCHEMA_VERSION and had_schema and path is not None:
            backup_path = _migration_backup(conn, path, current_version)
        schema_needs_install = current_version < SCHEMA_VERSION or not had_schema
        if schema_needs_install:
            conn.executescript(SCHEMA)
        if current_version < SCHEMA_VERSION:
            with writing(conn):
                # Another process may have completed this while we waited for the lock.
                if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                    migrate(conn)
                    if backup_path:
                        conn.execute(
                            "INSERT OR REPLACE INTO config(key,value) VALUES(?,?)",
                            ("last_migration_backup", backup_path),
                        )
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        # Schema and guards are immutable for a given schema version. Replaying
        # dozens of CREATE TABLE/TRIGGER statements on every invocation adds
        # latency and lock pressure without improving safety. `doctor` still
        # verifies their presence and migrations reinstall them when required.
        if schema_needs_install:
            _install_data_guards(conn)
            features.install_guards(conn)
        return conn
    except BaseException:
        conn.close()
        raise


@contextlib.contextmanager
def connection():
    """`with pt.connection() as conn:` -- guarantees conn.close() even if a
    query raises. A plain `conn = db(); ...; conn.close()` (used all over
    dashboard.py and mcp_server.py) leaks the connection on any exception in
    between; low-stakes for a one-off CLI call, real for dashboard.py's
    snapshot(), which runs in a loop for a session that can stay open for
    hours."""
    conn = db()
    try:
        yield conn
    finally:
        conn.close()


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
        "SELECT allow_short,allow_naked_options,max_gross_leverage,max_order_notional,"
        "max_daily_loss,max_drawdown,max_symbol_exposure,max_concentration"
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
        "max_daily_loss": float(row[4]) if row[4] is not None else None,
        "max_drawdown": float(row[5]) if row[5] is not None else None,
        "max_symbol_exposure": float(row[6]) if row[6] is not None else None,
        "max_concentration": float(row[7]) if row[7] is not None else None,
    }


def set_risk_limits(
    conn,
    account,
    allow_short=None,
    allow_naked_options=None,
    max_gross_leverage=None,
    max_order_notional=None,
    clear_max_order=False,
    max_daily_loss=None,
    max_drawdown=None,
    max_symbol_exposure=None,
    max_concentration=None,
    clear_daily_loss=False,
    clear_drawdown=False,
    clear_symbol_exposure=False,
    clear_concentration=False,
    source="cli",
    request_id=None,
):
    source, request_id = _context(source, request_id)
    details = {
        "allow_short": allow_short,
        "allow_naked_options": allow_naked_options,
        "max_gross_leverage": max_gross_leverage,
        "max_order_notional": max_order_notional,
        "max_daily_loss": max_daily_loss,
        "max_drawdown": max_drawdown,
        "max_symbol_exposure": max_symbol_exposure,
        "max_concentration": max_concentration,
        "clear_daily_loss": bool(clear_daily_loss),
        "clear_drawdown": bool(clear_drawdown),
        "clear_symbol_exposure": bool(clear_symbol_exposure),
        "clear_concentration": bool(clear_concentration),
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
    if max_order_notional is not None:
        max_order_notional = _money(max_order_notional)
    for value, label in (
        (max_daily_loss, "max daily loss"),
        (max_symbol_exposure, "max symbol exposure"),
    ):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise SystemExit(f"{label} must be a positive finite number")
    for value, label in (
        (max_drawdown, "max drawdown"),
        (max_concentration, "max concentration"),
    ):
        if value is not None and (not math.isfinite(value) or not 0 < value <= 1):
            raise SystemExit(f"{label} must be between 0 and 1")
    max_daily_loss = _money(max_daily_loss) if max_daily_loss is not None else None
    max_symbol_exposure = (
        _money(max_symbol_exposure) if max_symbol_exposure is not None else None
    )
    max_drawdown = _quantity(max_drawdown) if max_drawdown is not None else None
    max_concentration = (
        _quantity(max_concentration) if max_concentration is not None else None
    )
    if max_gross_leverage is not None:
        max_gross_leverage = _quantity(max_gross_leverage)
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
            "max_daily_loss": None
            if clear_daily_loss
            else current["max_daily_loss"]
            if max_daily_loss is None
            else max_daily_loss,
            "max_drawdown": None
            if clear_drawdown
            else current["max_drawdown"]
            if max_drawdown is None
            else max_drawdown,
            "max_symbol_exposure": None
            if clear_symbol_exposure
            else current["max_symbol_exposure"]
            if max_symbol_exposure is None
            else max_symbol_exposure,
            "max_concentration": None
            if clear_concentration
            else current["max_concentration"]
            if max_concentration is None
            else max_concentration,
        }
        conn.execute(
            "INSERT OR REPLACE INTO risk_settings"
            "(account,allow_short,allow_naked_options,max_gross_leverage,max_order_notional,"
            "max_daily_loss,max_drawdown,max_symbol_exposure,max_concentration)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                account,
                int(updated["allow_short"]),
                int(updated["allow_naked_options"]),
                updated["max_gross_leverage"],
                updated["max_order_notional"],
                updated["max_daily_loss"],
                updated["max_drawdown"],
                updated["max_symbol_exposure"],
                updated["max_concentration"],
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


_yf_rate_limited_until = 0.0  # monotonic timestamp; back off network calls until past this


def _yf_backoff_active():
    return time.monotonic() < _yf_rate_limited_until


def _yf_note_error(exc):
    """Track Yahoo Finance rate limiting so repeated calls back off instead of
    continuing to hammer an already-limited endpoint, which only extends the
    block. Matched by class name (not isinstance) so this works whether or
    not the caller imported yfinance.exceptions."""
    global _yf_rate_limited_until
    if type(exc).__name__ == "YFRateLimitError":
        _yf_rate_limited_until = time.monotonic() + 30.0


def _yf_rate_limit_error():
    wait = max(0, round(_yf_rate_limited_until - time.monotonic()))
    return SystemExit(f"Yahoo Finance is rate limiting requests — try again in ~{wait}s")


def option_price(occ):
    root, expiry, strike, cp = parse_occ(occ)
    if _yf_backoff_active():
        raise _yf_rate_limit_error()
    import yfinance as yf

    tk = yf.Ticker(root)
    try:
        exps = list(tk.options)
    except Exception as e:
        _yf_note_error(e)
        if _yf_backoff_active():
            raise _yf_rate_limit_error() from None
        raise SystemExit(f"could not load options for {root}: {e}") from None
    if expiry not in exps:
        raise SystemExit(
            f"{root} has no {expiry} expiry — available: {', '.join(exps[:8])}"
        )
    try:
        df = tk.option_chain(expiry)
    except Exception as e:
        _yf_note_error(e)
        if _yf_backoff_active():
            raise _yf_rate_limit_error() from None
        raise SystemExit(f"could not load chain for {root} {expiry}: {e}") from None
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


def _yahoo_live_price(symbol):
    _quiet_yf()
    if OCC_RE.match(symbol):
        return option_price(symbol)
    if _yf_backoff_active():
        raise _yf_rate_limit_error()
    import yfinance as yf

    try:
        p = yf.Ticker(symbol).fast_info["lastPrice"]
    except Exception as e:
        _yf_note_error(e)
        if _yf_backoff_active():
            raise _yf_rate_limit_error() from None
        p = None
    if not p or p <= 0:
        raise SystemExit(f"no price for {symbol} (unknown or delisted symbol?)")
    return float(p)


def live_price(symbol):
    symbol = symbol.upper()
    ttl = max(0.0, float(os.environ.get("PAPERTRADE_PRICE_CACHE_TTL", "15")))
    return features.provider_price(
        symbol, lambda: _yahoo_live_price(symbol), ttl=ttl, database=DB
    )


def _apply(state, symbol, side, qty, price):
    """Pure fill core. Mutates state={'cash','realized','pos':{sym:{qty,avg,mult,ac,margin}}}.
    Shared by the DB fill() and the equity-curve replay so the money math is identical."""
    if side not in ("buy", "sell"):
        raise SystemExit("side must be buy or sell")
    qty = _quantity(qty)
    price = _price(price)
    ac, mult, margin_per = classify(symbol)
    cash = _money(state["cash"])
    p = state["pos"].get(symbol)
    old_qty, old_avg, old_margin = (
        (p["qty"], p["avg"], p["margin"]) if p else (0.0, 0.0, 0.0)
    )
    signed = qty if side == "buy" else -qty
    new_qty = _quantity(old_qty + signed)
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

    state["cash"] = _money(cash)
    state["realized"] = _money(state["realized"] + realized)
    if abs(new_qty) < 1e-9:
        state["pos"].pop(symbol, None)
    else:
        state["pos"][symbol] = {
            "qty": _quantity(new_qty),
            "avg": _price(new_avg),
            "mult": mult,
            "ac": ac,
            "margin": _money(new_margin),
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
    position = after["pos"].get(symbol)
    symbol_exposure = 0.0
    if position:
        symbol_exposure = (
            abs(position["margin"])
            if position["ac"] == "future"
            else abs(position["qty"] * position["mult"] * price)
        )
    concentration = symbol_exposure / gross_after if gross_after > 0 else 0.0
    if (
        not reason
        and increasing
        and limits["max_symbol_exposure"] is not None
        and symbol_exposure > limits["max_symbol_exposure"] + 1e-9
    ):
        reason = (
            f"symbol exposure {symbol_exposure:,.2f} exceeds limit "
            f"{limits['max_symbol_exposure']:,.2f}"
        )
    if (
        not reason
        and increasing
        and limits["max_concentration"] is not None
        and concentration > limits["max_concentration"] + 1e-9
    ):
        reason = (
            f"symbol concentration {concentration:.1%} exceeds limit "
            f"{limits['max_concentration']:.1%}"
        )
    today = datetime.now(timezone.utc).date().isoformat()
    income = conn.execute(
        "SELECT COALESCE(SUM(e.amount),0) FROM ledger_entries e"
        " JOIN ledger_transactions t ON t.id=e.transaction_id"
        " WHERE t.account=? AND substr(t.ts,1,10)=?"
        " AND e.book IN ('income:realized','expense:commission')",
        (account, today),
    ).fetchone()[0]
    daily_pnl_after = _money(-income + after.get("realized", 0))
    daily_loss_after = max(0.0, -daily_pnl_after)
    if (
        not reason
        and increasing
        and limits["max_daily_loss"] is not None
        and daily_loss_after > limits["max_daily_loss"] + 1e-9
    ):
        reason = (
            f"daily loss {daily_loss_after:,.2f} exceeds limit "
            f"{limits['max_daily_loss']:,.2f}"
        )
    peak_row = conn.execute(
        "SELECT peak FROM equity_peaks WHERE account=?", (account,)
    ).fetchone()
    peak_equity = max(equity_before, peak_row[0] if peak_row else equity_before)
    drawdown = (
        max(0.0, (peak_equity - equity_after) / peak_equity)
        if peak_equity > 0
        else 0.0
    )
    if (
        not reason
        and increasing
        and limits["max_drawdown"] is not None
        and drawdown > limits["max_drawdown"] + 1e-9
    ):
        reason = (
            f"drawdown {drawdown:.1%} exceeds limit "
            f"{limits['max_drawdown']:.1%}"
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
        "symbol_exposure_after": symbol_exposure,
        "symbol_concentration_after": concentration,
        "daily_loss_after": daily_loss_after,
        "drawdown_after": drawdown,
        "peak_equity": peak_equity,
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


def _fill_locked(
    conn,
    account,
    symbol,
    side,
    qty,
    price,
    enforce_risk=True,
    simulate_execution=None,
):
    """Apply a fill while the caller owns a writing() transaction."""
    before = _portfolio_state_locked(conn, account)
    if simulate_execution is None:
        simulate_execution = enforce_risk
    execution = (
        features.realistic_fill(conn, account, side, qty, price)
        if simulate_execution
        else {
            "requested_quantity": qty,
            "filled_quantity": qty,
            "remaining_quantity": 0.0,
            "fill_price": _price(price),
            "slippage": 0.0,
            "commission": 0.0,
            "partial": False,
        }
    )
    qty = execution["filled_quantity"]
    price = execution["fill_price"]
    if qty <= 0:
        raise SystemExit("execution settings produced a zero fill")
    state = _clone_state(before)
    _apply(state, symbol, side, qty, price)
    commission = execution["commission"]
    if commission:
        state["cash"] = _money(state["cash"] - commission)
        state["realized"] = _money(state["realized"] - commission)
        if state["cash"] < 0:
            raise SystemExit("insufficient cash for execution commission")
    report = _risk_report_locked(conn, account, symbol, side, qty, price, before, state)
    report["execution"] = execution
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
    current_realized = conn.execute(
        "SELECT realized FROM accounts WHERE name=?", (account,)
    ).fetchone()[0]
    conn.execute(
        "UPDATE accounts SET cash=?, realized=? WHERE name=?",
        (state["cash"], _money(current_realized + state["realized"]), account),
    )
    features.record_fill(
        conn, account, symbol, side, qty, price, before, state, commission
    )
    conn.execute(
        "INSERT INTO equity_peaks(account,peak,updated) VALUES(?,?,?)"
        " ON CONFLICT(account) DO UPDATE SET peak=max(peak,excluded.peak),"
        " updated=excluded.updated",
        (
            account,
            max(report["equity_before"], report["equity_after"]),
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
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
    qty = _quantity(qty)
    limit = _price(limit) if limit is not None else None
    filled_price = _price(filled_price) if filled_price is not None else None
    stop_price = _price(stop_price) if stop_price is not None else None
    trail_price = _price(trail_price) if trail_price is not None else None
    trail_percent = _quantity(trail_percent) if trail_percent is not None else None
    hwm = _price(hwm) if hwm is not None else None
    notional = _money(notional) if notional is not None else None
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
            report = _fill_locked(conn, account, symbol, side, qty, price)
            execution = report["execution"]
            status = "partially_filled" if execution["partial"] else "filled"
            oid = _insert_order_locked(
                conn,
                account,
                symbol,
                side,
                qty,
                None,
                status,
                execution["fill_price"],
                ts,
                source,
                request_id,
            )
            conn.execute(
                "UPDATE orders SET filled_qty=?,commission=?,slippage=? WHERE id=?",
                (
                    execution["filled_quantity"],
                    execution["commission"],
                    execution["slippage"],
                    oid,
                ),
            )
            _audit_locked(
                conn,
                "order.place",
                account,
                source,
                request_id,
                {
                    **intent,
                    "order_id": oid,
                    "filled_price": execution["fill_price"],
                    "filled_quantity": execution["filled_quantity"],
                },
            )
        print(
            f"{status} #{oid} {side} {execution['filled_quantity']:g} "
            f"{symbol} @ {execution['fill_price']:.2f}"
        )
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
        execution = None
        if immediate:
            report = _fill_locked(conn, account, symbol, side, qty, price)
            execution = report["execution"]
            status = "partially_filled" if execution["partial"] else "filled"
        oid = _insert_order_locked(
            conn,
            account,
            symbol,
            side,
            qty,
            limit_price,
            status,
            execution["fill_price"] if execution else None,
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
        if execution:
            conn.execute(
                "UPDATE orders SET filled_qty=?,commission=?,slippage=? WHERE id=?",
                (
                    execution["filled_quantity"],
                    execution["commission"],
                    execution["slippage"],
                    oid,
                ),
            )
        if order_class in ("bracket", "oto"):
            _linked_exit_orders_locked(
                conn,
                oid,
                account,
                symbol,
                side,
                execution["filled_quantity"] if execution else qty,
                take_profit,
                stop_loss,
                "pending" if immediate else "held",
                ts,
                source,
                order_class,
            )
        _audit_locked(conn, "order.submit", account, source, request_id, intent)
    if status in ("filled", "partially_filled"):
        print(
            f"{status} #{oid} {side} "
            f"{execution['filled_quantity'] if execution else qty:g} "
            f"{symbol} @ {execution['fill_price'] if execution else price:.2f}"
        )
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
    amount = _money(amount)
    if amount == 0:
        raise SystemExit("cash adjustment rounds to zero at cent precision")
    source, request_id = _context(source, request_id)
    details = {"amount": amount}
    with writing(conn):
        if _idempotent_action(
            conn, "cash.adjust", account, source, request_id, details
        ):
            print(f"idempotent replay: cash adjustment {amount:+,.2f} for {account}")
            return
        row = conn.execute(
            "SELECT cash,deposits FROM accounts WHERE name=?", (account,)
        ).fetchone()
        if not row:
            raise SystemExit(f"no account '{account}'")
        if amount < 0 and row[0] + amount < 0:
            raise SystemExit(f"insufficient cash: have {row[0]:,.2f}")
        new_cash = _money(row[0] + amount)
        new_deposits = _money(row[1] + amount)
        conn.execute(
            "UPDATE accounts SET cash=?, deposits=? WHERE name=?",
            (new_cash, new_deposits, account),
        )
        conn.execute(
            "INSERT INTO cashflow(account, ts, amount) VALUES(?,?,?)",
            (account, datetime.now(timezone.utc).isoformat(timespec="seconds"), amount),
        )
        _audit_locked(conn, "cash.adjust", account, source, request_id, details)
        features.record_cash(conn, account, amount)
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
    cash = _money(cash)
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
        conn.execute("INSERT INTO execution_settings(account) VALUES(?)", (name,))
        if (
            make_default
            and not conn.execute(
                "SELECT 1 FROM config WHERE key='default_account'"
            ).fetchone()
        ):
            conn.execute("INSERT INTO config VALUES('default_account',?)", (name,))
            made_default = True
        _audit_locked(conn, "account.create", name, source, request_id, details)
        features.record_cash(conn, name, cash, "account.open")
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
        conn.execute(
            "INSERT INTO accounts(name,cash,deposits,realized,created)"
            " SELECT ?,cash,deposits,realized,created FROM accounts WHERE name=?",
            (new, old),
        )
        for tbl, col in [
            ("positions", "account"),
            ("orders", "account"),
            ("cashflow", "account"),
            ("risk_settings", "account"),
            ("corporate_actions", "account"),
            ("corporate_sync", "account"),
            ("watchlists", "account"),
            ("option_instructions", "account"),
            ("execution_settings", "account"),
            ("journal_entries", "account"),
            ("equity_peaks", "account"),
        ]:
            conn.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new, old))
        conn.execute("DELETE FROM accounts WHERE name=?", (old,))
        features.ensure_opening_ledger(conn, new)
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
    if reset_cash is not None:
        reset_cash = _money(reset_cash)
    source, request_id = _context(source, request_id)
    action = "account.delete" if reset_cash is None else "account.reset"
    details = {"reset_cash": reset_cash}
    with writing(conn):
        if _idempotent_action(conn, action, name, source, request_id, details):
            print(f"idempotent replay: {action} {name}")
            return
        if not conn.execute("SELECT 1 FROM accounts WHERE name=?", (name,)).fetchone():
            raise SystemExit(f"no account '{name}'")
        if reset_cash is None:
            features.close_ledger_account(conn, name)
        conn.execute("DELETE FROM positions WHERE account=?", (name,))
        conn.execute("DELETE FROM orders WHERE account=?", (name,))
        conn.execute("DELETE FROM corporate_actions WHERE account=?", (name,))
        conn.execute("DELETE FROM corporate_sync WHERE account=?", (name,))
        conn.execute("DELETE FROM option_instructions WHERE account=?", (name,))
        conn.execute("DELETE FROM journal_entries WHERE account=?", (name,))
        conn.execute("DELETE FROM execution_settings WHERE account=?", (name,))
        conn.execute("DELETE FROM equity_peaks WHERE account=?", (name,))
        conn.execute(
            "DELETE FROM watchlist_symbols WHERE watchlist_id IN"
            " (SELECT id FROM watchlists WHERE account=?)",
            (name,),
        )
        conn.execute("DELETE FROM watchlists WHERE account=?", (name,))
        if reset_cash is None:
            conn.execute("DELETE FROM cashflow WHERE account=?", (name,))
            conn.execute("DELETE FROM risk_settings WHERE account=?", (name,))
            conn.execute("DELETE FROM accounts WHERE name=?", (name,))
            conn.execute(
                "DELETE FROM config WHERE key='default_account' AND value=?", (name,)
            )
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
            features.reconcile(conn, name, repair=True)
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
                        _price(updates["hwm"]) if updates.get("hwm") is not None else None,
                        _price(updates["stop_price"])
                        if updates.get("stop_price") is not None
                        else None,
                        updates.get("triggered"),
                        oid,
                    ),
                )
            if not should_fill:
                print(f"#{oid} {current['symbol']}: price {price:.2f}, {description}")
                continue
            try:
                report = _fill_locked(
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
            execution = report["execution"]
            terminal_status = (
                "partially_filled" if execution["partial"] else "filled"
            )
            conn.execute(
                "UPDATE orders SET status=?,filled_price=?,filled_qty=?,"
                "commission=?,slippage=? WHERE id=? AND status='pending'",
                (
                    terminal_status,
                    execution["fill_price"],
                    execution["filled_quantity"],
                    execution["commission"],
                    execution["slippage"],
                    oid,
                ),
            )
            if execution["partial"]:
                conn.execute(
                    "UPDATE orders SET qty=? WHERE parent_id=? AND status='held'",
                    (execution["filled_quantity"], oid),
                )
            _linked_after_fill_locked(conn, current)
            _audit_locked(
                conn,
                "order.fill",
                current["account"],
                "engine",
                details={
                    "order_id": oid,
                    "filled_price": execution["fill_price"],
                    "filled_quantity": execution["filled_quantity"],
                },
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
        start=start,
        end=end,
        actions=True,
        auto_adjust=False,
        timeout=MARKET_DATA_TIMEOUT,
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
                    position = conn.execute(
                        "SELECT qty,avg_cost FROM positions"
                        " WHERE account=? AND symbol=?",
                        (name, symbol),
                    ).fetchone()
                    if not position:
                        continue
                    conn.execute(
                        "UPDATE positions SET qty=?,avg_cost=?"
                        " WHERE account=? AND symbol=?",
                        (
                            _quantity(position[0] * value),
                            _price(position[1] / value),
                            name,
                            symbol,
                        ),
                    )
                elif kind == "dividend":
                    qty = _position_qty_before(conn, name, symbol, action_date)
                    cash_effect = _money(qty * value)
                    balances = conn.execute(
                        "SELECT cash,realized FROM accounts WHERE name=?", (name,)
                    ).fetchone()
                    conn.execute(
                        "UPDATE accounts SET cash=?,realized=? WHERE name=?",
                        (
                            _money(balances[0] + cash_effect),
                            _money(balances[1] + cash_effect),
                            name,
                        ),
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
        frame = yf.Ticker(symbol).history(
            **kwargs, timeout=MARKET_DATA_TIMEOUT
        ).tail(limit)
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


def latest_quote(symbol, detailed=True):
    symbol = symbol.strip().upper()
    price = live_price(symbol)
    info = {}
    if detailed:
        # `.info` is a second, comparatively expensive Yahoo request. Commands
        # that only need a current mark use the fast indicative path.
        _quiet_yf()
        import yfinance as yf

        try:
            info = yf.Ticker(symbol).info or {}
        except Exception:
            info = {}
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
    quote = latest_quote(symbol, detailed=False)
    return {
        "symbol": quote["symbol"],
        "price": quote["last"],
        "timestamp": quote["timestamp"],
    }


def market_snapshot(symbol):
    history = market_history(symbol, "bars", timeframe="1Day", limit=2)["data"]
    quote = latest_quote(symbol, detailed=False)
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
    import tempfile

    directory = directory or os.path.expanduser("~/.papertrade_backups")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    descriptor, path = tempfile.mkstemp(
        prefix=f"papertrade-{stamp}-", suffix=".db", dir=directory
    )
    os.close(descriptor)
    os.chmod(path, 0o600)
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
    schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
    logical = _database_invariants(conn)
    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    guard_count = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master"
        " WHERE type='trigger' AND name LIKE 'guard_%'"
    ).fetchone()[0]
    backup = conn.execute(
        "SELECT value FROM config WHERE key='last_migration_backup'"
    ).fetchone()
    feature_status = features.feature_health(conn)
    healthy = (
        integrity == "ok"
        and schema_version == SCHEMA_VERSION
        and logical["total"] == 0
        and bool(foreign_keys)
        and guard_count >= EXPECTED_DATA_GUARDS
        and feature_status["valid"]
    )
    return {
        "status": "ok" if healthy else "degraded",
        "integrity": integrity,
        "logical_integrity": logical,
        "foreign_keys": bool(foreign_keys),
        "data_guards": guard_count,
        "expected_data_guards": EXPECTED_DATA_GUARDS,
        "fixed_precision": {"money": 2, "price": 6, "quantity": 8},
        "features": feature_status,
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "schema_version": schema_version,
        "expected_schema_version": SCHEMA_VERSION,
        "last_migration_backup": os.path.basename(backup[0]) if backup else None,
        "accounts": conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
        "positions": conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0],
        "pending_orders": conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending'"
        ).fetchone()[0],
        "held_orders": conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='held'"
        ).fetchone()[0],
        "watchlists": conn.execute("SELECT COUNT(*) FROM watchlists").fetchone()[0],
        "database": os.path.basename(DB),
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
    from concurrent.futures import ThreadPoolExecutor

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
                timeout=MARKET_DATA_TIMEOUT,
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
    # this is sized to the task count instead of a fixed small pool. Capped
    # at 16, not unbounded, so a very large portfolio doesn't open dozens of
    # concurrent connections/threads for one CLI call.
    with ThreadPoolExecutor(max_workers=min(16, len(ordered))) as executor:
        return dict(executor.map(fetch, ordered))


def _grouped_option_prices(root, expiry, legs, ignore_errors):
    """One option_chain() download priced across every leg on that
    underlying/expiry, instead of option_price()'s one-download-per-leg.
    Mirrors option_price()'s error handling exactly (same rate-limit
    backoff, same clean SystemExit messages) since it's the same fetch,
    just shared across legs that would otherwise each redo it."""
    if _yf_backoff_active():
        if ignore_errors:
            return {sym: None for sym, _s, _c in legs}
        raise _yf_rate_limit_error()
    import yfinance as yf

    tk = yf.Ticker(root)
    try:
        exps = list(tk.options)
    except Exception as e:
        _yf_note_error(e)
        if ignore_errors:
            return {sym: None for sym, _s, _c in legs}
        if _yf_backoff_active():
            raise _yf_rate_limit_error() from None
        raise SystemExit(f"could not load options for {root}: {e}") from None
    if expiry not in exps:
        if ignore_errors:
            return {sym: None for sym, _s, _c in legs}
        raise SystemExit(
            f"{root} has no {expiry} expiry — available: {', '.join(exps[:8])}"
        )
    try:
        chain = tk.option_chain(expiry)
    except Exception as e:
        _yf_note_error(e)
        if ignore_errors:
            return {sym: None for sym, _s, _c in legs}
        if _yf_backoff_active():
            raise _yf_rate_limit_error() from None
        raise SystemExit(f"could not load chain for {root} {expiry}: {e}") from None

    out = {}
    for sym, strike, cp in legs:
        df = chain.calls if cp == "C" else chain.puts
        rows = df[df.strike == strike]
        if rows.empty:
            if ignore_errors:
                out[sym] = None
                continue
            raise SystemExit(f"no {strike:g} strike for {root} {expiry}")
        r = rows.iloc[0]
        bid, ask, last = float(r.bid), float(r.ask), float(r.lastPrice)
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
        if mid <= 0:
            if ignore_errors:
                out[sym] = None
                continue
            raise SystemExit(f"no tradeable price for {sym}")
        out[sym] = mid
    return out


def batch_prices(symbols, price_fn=None, ignore_errors=False):
    """Fetch unique live marks concurrently while preserving input order."""
    from concurrent.futures import ThreadPoolExecutor

    ordered = list(dict.fromkeys(symbols))
    price_fn = price_fn or live_price

    # Only the real default price_fn gets the chain-grouping fast path below
    # -- a custom price_fn (heavily used by tests to mock prices without
    # touching yfinance) must still be called once per symbol, unchanged.
    if price_fn is live_price:
        stocks, groups = [], {}
        for s in ordered:
            if OCC_RE.match(s):
                root, expiry, strike, cp = parse_occ(s)
                groups.setdefault((root, expiry), []).append((s, strike, cp))
            else:
                stocks.append(s)

        def fetch_stock(s):
            try:
                return {s: live_price(s)}
            except SystemExit:
                if ignore_errors:
                    return {s: None}
                raise

        tasks = [(fetch_stock, (s,)) for s in stocks]
        tasks += [
            (_grouped_option_prices, (root, expiry, legs, ignore_errors))
            for (root, expiry), legs in groups.items()
        ]
        if len(tasks) < 2:
            out = {}
            for fn, fn_args in tasks:
                out.update(fn(*fn_args))
            return out
        out = {}
        with ThreadPoolExecutor(max_workers=min(16, len(tasks))) as executor:
            futures = [executor.submit(fn, *fn_args) for fn, fn_args in tasks]
            for fut in futures:
                out.update(fut.result())  # re-raises on failure, same as before
        return out

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
    # queuing behind a small fixed pool — capped at 16, not unbounded, so a
    # very large portfolio doesn't open dozens of concurrent connections.
    with ThreadPoolExecutor(max_workers=min(16, len(ordered))) as executor:
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


def tracked_symbols(conn, account=None):
    params = ()
    position_where = ""
    watchlist_where = ""
    if account:
        position_where = " WHERE account=?"
        watchlist_where = " WHERE w.account=?"
        params = (account,)
    symbols = {
        row[0]
        for row in conn.execute(
            f"SELECT symbol FROM positions{position_where}", params
        )
    }
    symbols.update(
        row[0]
        for row in conn.execute(
            "SELECT ws.symbol FROM watchlist_symbols ws"
            " JOIN watchlists w ON w.id=ws.watchlist_id"
            f"{watchlist_where}",
            params,
        )
    )
    symbols.update(row[0] for row in conn.execute("SELECT symbol FROM alert_rules"))
    return sorted(symbols)


def warm_quotes(conn, symbols=None, account=None):
    selected = sorted(
        {symbol.strip().upper() for symbol in (symbols or []) if symbol.strip()}
    )
    if not selected:
        selected = tracked_symbols(conn, account)
    if not selected:
        return {"requested": 0, "updated": 0, "failed": [], "quotes": {}}
    prices = batch_prices(selected, ignore_errors=True)
    return {
        "requested": len(selected),
        "updated": sum(price is not None for price in prices.values()),
        "failed": [symbol for symbol, price in prices.items() if price is None],
        "quotes": {symbol: price for symbol, price in prices.items() if price is not None},
    }


def simulation_report(conn, account):
    account_row = conn.execute(
        "SELECT cash,deposits,realized,created FROM accounts WHERE name=?",
        (account,),
    ).fetchone()
    if not account_row:
        raise SystemExit(f"no account '{account}'")
    cash, deposits, realized, created = account_row
    positions = conn.execute(
        "SELECT symbol,qty,avg_cost,mult FROM positions WHERE account=?"
        " ORDER BY symbol",
        (account,),
    ).fetchall()
    cached = features.cached_prices(DB, [row[0] for row in positions])
    fallback = {symbol: avg for symbol, _qty, avg, _mult in positions}
    equity = current_equity(
        conn,
        account,
        price_fn=lambda symbol: cached.get(symbol, {}).get(
            "price", fallback[symbol]
        ),
    )
    pending = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE account=? AND status IN ('pending','held')",
        (account,),
    ).fetchone()[0]
    fills = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE account=?"
        " AND status IN ('filled','settled','exercised','partially_filled')",
        (account,),
    ).fetchone()[0]
    return {
        "kind": "local-paper-trading-summary",
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": account,
        "account_created": created,
        "cash": _money(cash),
        "deposits": _money(deposits),
        "realized": _money(realized),
        "equity": _money(equity),
        "return": round((equity / deposits - 1) if deposits else 0, 8),
        "positions": len(positions),
        "cached_marks": len(cached),
        "stale_marks": sum(mark["stale"] for mark in cached.values()),
        "pending_orders": pending,
        "completed_orders": fills,
        "ledger": features.reconcile(conn, account),
        "live_execution": False,
    }


def save_simulation_report(report, output=None):
    if output:
        path = os.path.abspath(os.path.expanduser(output))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    else:
        directory = os.path.expanduser("~/.papertrade_reports")
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        path = os.path.join(directory, f"{report['account']}-{stamp}.json")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return path


def terminal_benchmark(iterations=10):
    import subprocess

    iterations = max(3, min(int(iterations), 100))
    startup = []
    database = []
    command = [sys.executable, "-m", "papertrade", "--version"]
    for _ in range(iterations):
        started = time.perf_counter()
        subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        startup.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        conn = db()
        conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
        conn.close()
        database.append((time.perf_counter() - started) * 1000)
    startup.sort()
    database.sort()
    middle = iterations // 2
    return {
        "iterations": iterations,
        "startup_median_ms": round(startup[middle], 3),
        "startup_p95_ms": round(startup[min(iterations - 1, math.ceil(iterations * 0.95) - 1)], 3),
        "database_median_ms": round(database[middle], 3),
        "database_p95_ms": round(database[min(iterations - 1, math.ceil(iterations * 0.95) - 1)], 3),
        "budgets_ms": {"startup": 75, "database": 10},
        "passes": {
            "startup": startup[middle] < 75,
            "database": database[middle] < 10,
        },
    }


def completion_script(shell):
    commands = sorted(
        {
            "new", "accounts", "use", "buy", "sell", "order", "position",
            "option", "watchlist", "data", "quotes", "alert", "report",
            "automation", "benchmark", "doctor", "dash", "completion",
        }
    )
    words = " ".join(commands)
    if shell == "bash":
        return (
            "_tradingcli_complete() { COMPREPLY=( $(compgen -W '"
            + words
            + "' -- \"${COMP_WORDS[COMP_CWORD]}\") ); }\n"
            "complete -F _tradingcli_complete tradingcli\n"
        )
    if shell == "zsh":
        return (
            "#compdef tradingcli\n"
            "_tradingcli() { local -a commands; commands=("
            + words
            + "); _describe 'command' commands; }\ncompdef _tradingcli tradingcli\n"
        )
    if shell == "fish":
        return f"complete -c tradingcli -f -a '{words}'\n"
    raise SystemExit("shell must be bash, zsh, or fish")


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
        "ledger",
        "execution",
        "journal",
        "automation",
        "quotes",
        "alert",
        "report",
        "benchmark",
        "completion",
        "strategy",
        "broker",
        "security",
        "serve",
    ],
}


def _build_parser():
    p = argparse.ArgumentParser(
        prog="tradingcli",
        epilog="Global automation flags: --json --csv --quiet --schema --help-all",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
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
    risk.add_argument("--max-daily-loss", type=float)
    risk.add_argument("--max-drawdown", type=float)
    risk.add_argument("--max-symbol-exposure", type=float)
    risk.add_argument("--max-concentration", type=float)
    risk.add_argument("--clear-daily-loss", action="store_true")
    risk.add_argument("--clear-drawdown", action="store_true")
    risk.add_argument("--clear-symbol-exposure", action="store_true")
    risk.add_argument("--clear-concentration", action="store_true")
    actions = sub.add_parser("actions")
    actions.add_argument("-a", "--account")
    audit = sub.add_parser("audit")
    audit.add_argument("-a", "--account")
    audit.add_argument("--limit", type=int, default=50)
    export = sub.add_parser("export")
    export.add_argument("-a", "--account")
    export.add_argument("--limit", type=int, default=5000)
    backup = sub.add_parser("backup")
    backup_sub = backup.add_subparsers(dest="backup_cmd")
    backup_sub.add_parser("create")
    backup_sub.add_parser("list")
    prune = backup_sub.add_parser("prune")
    prune.add_argument("--keep", type=int, default=10)
    restore = backup_sub.add_parser("restore")
    restore.add_argument("path")
    restore.add_argument("--yes", action="store_true")

    ledger = sub.add_parser("ledger")
    ledger_sub = ledger.add_subparsers(dest="ledger_cmd", required=True)
    ledger_sub.add_parser("balances").add_argument("-a", "--account")
    reconcile_parser = ledger_sub.add_parser("reconcile")
    reconcile_parser.add_argument("-a", "--account")
    reconcile_parser.add_argument("--repair", action="store_true")
    entries = ledger_sub.add_parser("entries")
    entries.add_argument("-a", "--account")
    entries.add_argument("--limit", type=int, default=100)

    execution = sub.add_parser("execution")
    execution_sub = execution.add_subparsers(dest="execution_cmd", required=True)
    execution_sub.add_parser("get").add_argument("-a", "--account")
    execution_set = execution_sub.add_parser("set")
    execution_set.add_argument("-a", "--account")
    execution_set.add_argument("--commission-bps", type=float)
    execution_set.add_argument("--slippage-bps", type=float)
    execution_set.add_argument("--max-fill-quantity", type=float)
    execution_set.add_argument("--liquidity-fraction", type=float)
    execution_set.add_argument("--clear-max-fill", action="store_true")
    execution_preview = execution_sub.add_parser("preview")
    execution_preview.add_argument("side", choices=["buy", "sell"])
    execution_preview.add_argument("quantity", type=float)
    execution_preview.add_argument("price", type=float)
    execution_preview.add_argument("-a", "--account")

    journal = sub.add_parser("journal")
    journal_sub = journal.add_subparsers(dest="journal_cmd", required=True)
    journal_add = journal_sub.add_parser("add")
    journal_add.add_argument("title")
    journal_add.add_argument("--body", default="")
    journal_add.add_argument("--tags", default="")
    journal_add.add_argument("--symbol")
    journal_add.add_argument("--order-id", type=int)
    journal_add.add_argument("--attachment")
    journal_add.add_argument("-a", "--account")
    journal_list = journal_sub.add_parser("list")
    journal_list.add_argument("-a", "--account")
    journal_list.add_argument("--limit", type=int, default=100)
    journal_list.add_argument("--tag")
    journal_list.add_argument("--symbol")
    journal_sub.add_parser("attribution").add_argument("-a", "--account")
    journal_delete = journal_sub.add_parser("delete")
    journal_delete.add_argument("entry_id", type=int)
    journal_delete.add_argument("-a", "--account")

    automation = sub.add_parser("automation")
    automation_sub = automation.add_subparsers(dest="automation_cmd", required=True)
    automation_add = automation_sub.add_parser("add")
    automation_add.add_argument("name")
    automation_add.add_argument(
        "action",
        choices=["tick", "backup", "reconcile", "quotes", "alerts", "report"],
    )
    automation_add.add_argument("--interval-seconds", type=int, required=True)
    automation_add.add_argument("--account")
    automation_sub.add_parser("list")
    automation_sub.add_parser("run-due")
    automation_history = automation_sub.add_parser("history")
    automation_history.add_argument("--limit", type=int, default=100)

    quotes = sub.add_parser("quotes", help="local quote cache and streaming")
    quotes_sub = quotes.add_subparsers(dest="quotes_cmd", required=True)
    quotes_sub.add_parser("status")
    quotes_sub.add_parser("clear")
    quotes_warm = quotes_sub.add_parser("warm")
    quotes_warm.add_argument("symbols", nargs="*")
    quotes_warm.add_argument("-a", "--account")
    quotes_stream = quotes_sub.add_parser("stream")
    quotes_stream.add_argument("symbols", nargs="+")
    quotes_stream.add_argument("--interval", type=float, default=5)
    quotes_stream.add_argument("--count", type=int, default=0)
    quotes_stream.add_argument("--check-alerts", action="store_true")
    quotes_daemon = quotes_sub.add_parser("daemon")
    quotes_daemon.add_argument("-a", "--account")
    quotes_daemon.add_argument("--interval", type=float, default=15)
    quotes_daemon.add_argument("--count", type=int, default=0)
    quotes_daemon.add_argument("--check-alerts", action="store_true")

    alert = sub.add_parser("alert", help="local simulation price alerts")
    alert_sub = alert.add_subparsers(dest="alert_cmd", required=True)
    alert_add_parser = alert_sub.add_parser("add")
    alert_add_parser.add_argument("name")
    alert_add_parser.add_argument("symbol")
    threshold = alert_add_parser.add_mutually_exclusive_group(required=True)
    threshold.add_argument("--above", type=float)
    threshold.add_argument("--below", type=float)
    alert_add_parser.add_argument("--cooldown-seconds", type=int, default=300)
    alert_sub.add_parser("list")
    alert_delete_parser = alert_sub.add_parser("delete")
    alert_delete_parser.add_argument("alert_id", type=int)
    alert_check_parser = alert_sub.add_parser("check")
    alert_check_parser.add_argument("--notify", action="store_true")
    alert_events_parser = alert_sub.add_parser("events")
    alert_events_parser.add_argument("--limit", type=int, default=100)

    report = sub.add_parser("report", help="offline simulation reports")
    report_sub = report.add_subparsers(dest="report_cmd", required=True)
    report_summary = report_sub.add_parser("summary")
    report_summary.add_argument("-a", "--account")
    report_summary.add_argument("--save", action="store_true")
    report_summary.add_argument("--output")

    benchmark = sub.add_parser("benchmark", help="measure local terminal performance")
    benchmark.add_argument("--iterations", type=int, default=10)

    completion = sub.add_parser("completion", help="generate shell completion")
    completion.add_argument("shell", choices=["bash", "zsh", "fish"])

    strategy = sub.add_parser("strategy")
    strategy_sub = strategy.add_subparsers(dest="strategy_cmd", required=True)
    walk_forward = strategy_sub.add_parser("walk-forward")
    walk_forward.add_argument("symbol")

    broker = sub.add_parser("broker")
    broker_sub = broker.add_subparsers(dest="broker_cmd", required=True)
    broker_export_parser = broker_sub.add_parser("export")
    broker_export_parser.add_argument("broker", choices=["generic", "alpaca", "ibkr"])
    broker_export_parser.add_argument("-a", "--account")
    broker_import_parser = broker_sub.add_parser("import")
    broker_import_parser.add_argument("broker", choices=["generic", "alpaca", "ibkr"])
    broker_import_parser.add_argument("path")
    broker_import_parser.add_argument("-a", "--account")

    security = sub.add_parser("security")
    security_sub = security.add_subparsers(dest="security_cmd", required=True)
    encrypt = security_sub.add_parser("encrypt-copy")
    encrypt.add_argument("output")
    encrypt.add_argument(
        "--password-env", default="PAPERTRADE_ENCRYPTION_PASSWORD"
    )
    decrypt = security_sub.add_parser("decrypt-copy")
    decrypt.add_argument("input")
    decrypt.add_argument("output")
    decrypt.add_argument(
        "--password-env", default="PAPERTRADE_ENCRYPTION_PASSWORD"
    )

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--token-env", default="PAPERTRADE_API_TOKEN")
    serve.add_argument("--allow-mutations", action="store_true")
    sub.add_parser("doctor")
    dash = sub.add_parser("dash")
    dash.add_argument("-a", "--account")
    dash.add_argument("-n", "--interval", type=float)
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
        dash_argv = [sys.executable, script]
        if args.account:
            dash_argv += ["-a", args.account]
        if args.interval is not None:
            dash_argv += ["-n", str(args.interval)]
        os.execv(sys.executable, dash_argv)
    if args.cmd == "completion":
        print(completion_script(args.shell), end="")
        return
    if args.cmd == "benchmark":
        print(json.dumps(terminal_benchmark(args.iterations), indent=2))
        return
    if args.cmd == "chain":
        show_chain(args.underlying, args.expiry)
        return
    if args.cmd in ("find", "validate", "quote", "market", "calendar", "data", "asset"):
        if args.cmd == "find":
            result = search_assets(args.query, args.limit)
        elif args.cmd == "validate":
            result = validate_asset(args.symbol)
        elif args.cmd == "quote":
            result = latest_quote(args.symbol, detailed=False)
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
            result = latest_quote(args.symbol, detailed=False)
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
            result = latest_quote(f"{pair}=X", detailed=False)
        print(json.dumps(result, indent=2))
        return
    if args.cmd == "serve":
        token = os.environ.get(args.token_env, "")
        features.serve_api(db, args.host, args.port, token, args.allow_mutations)
        return
    if args.cmd == "security":
        password = os.environ.get(args.password_env, "")
        if args.security_cmd == "encrypt-copy":
            result = features.encrypt_database_copy(DB, args.output, password)
        else:
            result = features.decrypt_database_copy(args.input, args.output, password)
        print(json.dumps(result, indent=2))
        return
    if args.cmd == "backup" and args.backup_cmd == "restore":
        if not args.yes:
            raise SystemExit("backup restore requires --yes")
        current = db()
        try:
            safety = backup_database(current)
        finally:
            current.close()
        result = features.restore_backup(DB, args.path, SCHEMA_VERSION)
        result["pre_restore_backup"] = os.path.basename(safety)
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
            expanded = (
                args.max_daily_loss,
                args.max_drawdown,
                args.max_symbol_exposure,
                args.max_concentration,
            )
            clear_expanded = (
                args.clear_daily_loss,
                args.clear_drawdown,
                args.clear_symbol_exposure,
                args.clear_concentration,
            )
            if (
                any(value is not None for value in changes + expanded)
                or args.clear_max_order
                or any(clear_expanded)
            ):
                with contextlib.redirect_stdout(io.StringIO()):
                    set_risk_limits(
                        conn,
                        account,
                        *changes,
                        clear_max_order=args.clear_max_order,
                        max_daily_loss=args.max_daily_loss,
                        max_drawdown=args.max_drawdown,
                        max_symbol_exposure=args.max_symbol_exposure,
                        max_concentration=args.max_concentration,
                        clear_daily_loss=args.clear_daily_loss,
                        clear_drawdown=args.clear_drawdown,
                        clear_symbol_exposure=args.clear_symbol_exposure,
                        clear_concentration=args.clear_concentration,
                    )
                print(json.dumps(risk_limits(conn, account), indent=2))
            else:
                print(json.dumps(risk_limits(conn, account), indent=2))
        elif args.cmd == "ledger":
            account = resolve_account(conn, args.account)
            if args.ledger_cmd == "balances":
                result = features.ledger_balance(conn, account)
            elif args.ledger_cmd == "reconcile":
                with writing(conn):
                    result = features.reconcile(conn, account, args.repair)
            else:
                rows = conn.execute(
                    "SELECT t.id,t.ts,t.kind,t.reference_type,t.reference_id,"
                    "e.book,e.amount,e.commodity,e.units,e.price"
                    " FROM ledger_transactions t JOIN ledger_entries e"
                    " ON e.transaction_id=t.id WHERE t.account=?"
                    " ORDER BY t.id DESC,e.id LIMIT ?",
                    (account, max(1, min(args.limit, 5000))),
                ).fetchall()
                result = [
                    {
                        "transaction_id": row[0],
                        "timestamp": row[1],
                        "kind": row[2],
                        "reference_type": row[3],
                        "reference_id": row[4],
                        "book": row[5],
                        "amount": row[6],
                        "commodity": row[7],
                        "units": row[8],
                        "price": row[9],
                    }
                    for row in rows
                ]
            print(json.dumps(result, indent=2))
        elif args.cmd == "execution":
            account = resolve_account(conn, args.account)
            if args.execution_cmd == "get":
                result = features.execution_settings(conn, account)
            elif args.execution_cmd == "set":
                with writing(conn):
                    result = features.set_execution_settings(
                        conn,
                        account,
                        args.commission_bps,
                        args.slippage_bps,
                        args.max_fill_quantity,
                        args.liquidity_fraction,
                        args.clear_max_fill,
                    )
            else:
                result = features.realistic_fill(
                    conn, account, args.side, args.quantity, args.price
                )
            print(json.dumps(result, indent=2))
        elif args.cmd == "journal":
            account = resolve_account(conn, args.account)
            if args.journal_cmd == "add":
                with writing(conn):
                    entry_id = features.journal_add(
                        conn,
                        account,
                        args.title,
                        args.body,
                        args.tags.split(","),
                        args.symbol,
                        args.order_id,
                        args.attachment,
                    )
                result = {"id": entry_id}
            elif args.journal_cmd == "list":
                result = features.journal_list(
                    conn, account, args.limit, args.tag, args.symbol
                )
            elif args.journal_cmd == "attribution":
                result = features.performance_attribution(conn, account)
            else:
                with writing(conn):
                    features.journal_delete(conn, account, args.entry_id)
                result = {"deleted": args.entry_id}
            print(json.dumps(result, indent=2))
        elif args.cmd == "automation":
            if args.automation_cmd == "add":
                payload = {"account": args.account} if args.account else {}
                with writing(conn):
                    features.schedule_add(
                        conn,
                        args.name,
                        args.action,
                        args.interval_seconds,
                        payload,
                    )
                result = {"created": args.name}
            elif args.automation_cmd == "list":
                result = features.schedule_list(conn)
            elif args.automation_cmd == "history":
                result = [
                    {
                        "id": row[0],
                        "job_id": row[1],
                        "started": row[2],
                        "finished": row[3],
                        "status": row[4],
                        "output": json.loads(row[5]) if row[5] else None,
                        "error": row[6],
                    }
                    for row in conn.execute(
                        "SELECT id,job_id,started,finished,status,output,error"
                        " FROM automation_runs ORDER BY id DESC LIMIT ?",
                        (max(1, min(args.limit, 1000)),),
                    )
                ]
            else:
                def execute_automation(action, payload):
                    if action == "tick":
                        tick(conn)
                        return {"ticked": True}
                    if action == "backup":
                        return {"backup": os.path.basename(backup_database(conn))}
                    if action == "quotes":
                        return warm_quotes(conn, account=payload.get("account"))
                    if action == "alerts":
                        rules = features.alert_list(conn)
                        prices = batch_prices(
                            sorted({rule["symbol"] for rule in rules}),
                            ignore_errors=True,
                        )
                        with writing(conn):
                            events = features.alert_check(conn, prices)
                        return {"triggered": events}
                    if action == "report":
                        account = resolve_account(conn, payload.get("account"))
                        report = simulation_report(conn, account)
                        return {
                            "report": os.path.basename(
                                save_simulation_report(report)
                            )
                        }
                    account = resolve_account(conn, payload.get("account"))
                    with writing(conn):
                        return features.reconcile(conn, account, repair=True)

                result = features.run_due(conn, execute_automation)
            print(json.dumps(result, indent=2))
        elif args.cmd == "quotes":
            if args.quotes_cmd == "status":
                result = features.cache_status(conn)
                result["entries"] = features.cached_prices(DB)
                print(json.dumps(result, indent=2))
            elif args.quotes_cmd == "clear":
                with writing(conn):
                    removed = conn.execute(
                        "DELETE FROM market_cache WHERE kind='price'"
                    ).rowcount
                features._price_cache.clear()
                print(json.dumps({"removed": removed}, indent=2))
            elif args.quotes_cmd == "warm":
                print(
                    json.dumps(
                        warm_quotes(conn, args.symbols, args.account),
                        indent=2,
                    )
                )
            else:
                interval = max(0.1, float(args.interval))
                remaining = max(0, int(args.count))
                iteration = 0
                while remaining == 0 or iteration < remaining:
                    symbols = (
                        args.symbols
                        if args.quotes_cmd == "stream"
                        else tracked_symbols(conn, args.account)
                    )
                    result = warm_quotes(conn, symbols)
                    events = []
                    if args.check_alerts:
                        with writing(conn):
                            events = features.alert_check(conn, result["quotes"])
                    payload = {
                        "timestamp": datetime.now(timezone.utc).isoformat(
                            timespec="seconds"
                        ),
                        **result,
                        "alerts": events,
                    }
                    print(json.dumps(payload, separators=(",", ":")), flush=True)
                    iteration += 1
                    if remaining and iteration >= remaining:
                        break
                    time.sleep(interval)
        elif args.cmd == "alert":
            if args.alert_cmd == "add":
                condition = "above" if args.above is not None else "below"
                threshold = args.above if args.above is not None else args.below
                with writing(conn):
                    alert_id = features.alert_add(
                        conn,
                        args.name,
                        args.symbol,
                        condition,
                        threshold,
                        args.cooldown_seconds,
                    )
                result = {"id": alert_id, "created": args.name}
            elif args.alert_cmd == "list":
                result = features.alert_list(conn)
            elif args.alert_cmd == "delete":
                with writing(conn):
                    features.alert_delete(conn, args.alert_id)
                result = {"deleted": args.alert_id}
            elif args.alert_cmd == "events":
                result = features.alert_events(conn, args.limit)
            else:
                rules = features.alert_list(conn)
                prices = batch_prices(
                    sorted({rule["symbol"] for rule in rules}),
                    ignore_errors=True,
                )
                with writing(conn):
                    events = features.alert_check(conn, prices)
                notification_path = (
                    features.write_local_notifications(events)
                    if args.notify
                    else None
                )
                result = {
                    "checked": len(prices),
                    "triggered": events,
                    "notification_file": notification_path,
                }
            print(json.dumps(result, indent=2))
        elif args.cmd == "report":
            account = resolve_account(conn, args.account)
            result = simulation_report(conn, account)
            if args.save or args.output:
                result["saved_to"] = save_simulation_report(result, args.output)
            print(json.dumps(result, indent=2))
        elif args.cmd == "strategy":
            print(
                json.dumps(features.walk_forward_sma(args.symbol), indent=2)
            )
        elif args.cmd == "broker":
            account = resolve_account(conn, args.account)
            if args.broker_cmd == "export":
                print(features.broker_export(conn, account, args.broker), end="")
            else:
                path = os.path.abspath(os.path.expanduser(args.path))
                if not os.path.isfile(path):
                    raise SystemExit("broker import file does not exist")
                if os.path.getsize(path) > 20_000_000:
                    raise SystemExit("broker import is limited to 20 MB")
                with open(path, encoding="utf-8-sig", newline="") as handle:
                    content = handle.read()

                def imported_fill(
                    imported_account,
                    symbol,
                    side,
                    quantity,
                    price,
                    timestamp,
                    commission,
                ):
                    with writing(conn):
                        report = _fill_locked(
                            conn,
                            imported_account,
                            symbol,
                            side,
                            quantity,
                            price,
                            enforce_risk=False,
                        )
                        imported_commission = _money(commission)
                        if imported_commission:
                            balances = conn.execute(
                                "SELECT cash,realized FROM accounts WHERE name=?",
                                (imported_account,),
                            ).fetchone()
                            if balances[0] < imported_commission:
                                raise SystemExit(
                                    "insufficient cash for imported commission"
                                )
                            conn.execute(
                                "UPDATE accounts SET cash=?,realized=? WHERE name=?",
                                (
                                    _money(balances[0] - imported_commission),
                                    _money(balances[1] - imported_commission),
                                    imported_account,
                                ),
                            )
                            features.post_ledger(
                                conn,
                                imported_account,
                                "commission",
                                (
                                    {
                                        "book": "asset:cash",
                                        "amount": -imported_commission,
                                    },
                                    {
                                        "book": "expense:commission",
                                        "amount": imported_commission,
                                    },
                                ),
                                reference_type="symbol",
                                reference_id=symbol,
                            )
                        oid = _insert_order_locked(
                            conn,
                            imported_account,
                            symbol,
                            side,
                            quantity,
                            None,
                            "filled",
                            report["execution"]["fill_price"],
                            timestamp or datetime.now(timezone.utc).isoformat(
                                timespec="seconds"
                            ),
                            f"import:{args.broker}",
                            None,
                        )
                        conn.execute(
                            "UPDATE orders SET commission=? WHERE id=?",
                            (imported_commission, oid),
                        )

                result = features.broker_import(
                    conn,
                    account,
                    content,
                    args.broker,
                    imported_fill,
                    os.path.basename(path),
                )
                print(json.dumps(result, indent=2))
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
            if args.backup_cmd in (None, "create"):
                print(backup_database(conn))
            elif args.backup_cmd == "list":
                print(json.dumps(features.backup_inventory(DB), indent=2))
            else:
                removed = features.prune_backups(DB, args.keep)
                print(json.dumps({"removed": removed, "kept": args.keep}, indent=2))
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
