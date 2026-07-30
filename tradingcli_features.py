"""Operational feature layer for TradingCLI 0.4.

The core simulator remains in papertrade.py. This module owns optional
operational features with explicit boundaries: ledger/reconciliation, backup
operations, execution configuration, journal, automation, broker interchange,
walk-forward research, encrypted snapshots, and a loopback HTTP API.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile
import time
from urllib.parse import parse_qs, urlparse


FEATURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_transactions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account TEXT NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL,
  reference_type TEXT, reference_id TEXT, description TEXT,
  metadata TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS ledger_entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  transaction_id INTEGER NOT NULL, book TEXT NOT NULL,
  amount REAL NOT NULL, commodity TEXT NOT NULL DEFAULT 'USD',
  units REAL, price REAL);
CREATE INDEX IF NOT EXISTS idx_ledger_transactions_account
  ON ledger_transactions(account,id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_transaction
  ON ledger_entries(transaction_id);
CREATE TABLE IF NOT EXISTS execution_settings(
  account TEXT PRIMARY KEY, commission_bps REAL NOT NULL DEFAULT 0,
  slippage_bps REAL NOT NULL DEFAULT 0,
  max_fill_quantity REAL, liquidity_fraction REAL NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS journal_entries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT NOT NULL,
  ts TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
  tags TEXT NOT NULL DEFAULT '[]', symbol TEXT, order_id INTEGER,
  attachment TEXT, attachment_sha256 TEXT);
CREATE INDEX IF NOT EXISTS idx_journal_account_ts
  ON journal_entries(account,ts DESC);
CREATE TABLE IF NOT EXISTS automation_jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
  action TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  interval_seconds INTEGER NOT NULL, next_run TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS automation_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL,
  started TEXT NOT NULL, finished TEXT, status TEXT NOT NULL,
  output TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS idx_automation_due
  ON automation_jobs(enabled,next_run);
CREATE TABLE IF NOT EXISTS market_cache(
  provider TEXT NOT NULL, symbol TEXT NOT NULL, kind TEXT NOT NULL,
  fetched REAL NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL,
  PRIMARY KEY(provider,symbol,kind));
CREATE TABLE IF NOT EXISTS provider_health(
  provider TEXT PRIMARY KEY, last_success TEXT, last_failure TEXT,
  failures INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS equity_peaks(
  account TEXT PRIMARY KEY, peak REAL NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS broker_imports(
  fingerprint TEXT PRIMARY KEY, broker TEXT NOT NULL, imported TEXT NOT NULL,
  account TEXT NOT NULL, rows_imported INTEGER NOT NULL, source_name TEXT);
"""


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _money(value):
    return round(float(value) + 0.0, 2)


def install_schema(conn):
    conn.executescript(FEATURE_SCHEMA)


def prepare_migration(conn):
    """Install v5 tables and columns before core numeric normalization."""
    risk_columns = {row[1] for row in conn.execute("PRAGMA table_info(risk_settings)")}
    additions = (
        ("max_daily_loss", "REAL"),
        ("max_drawdown", "REAL"),
        ("max_symbol_exposure", "REAL"),
        ("max_concentration", "REAL"),
    )
    for name, ddl in additions:
        if name not in risk_columns:
            conn.execute(f"ALTER TABLE risk_settings ADD COLUMN {name} {ddl}")
    order_columns = {row[1] for row in conn.execute("PRAGMA table_info(orders)")}
    for name, ddl in (
        ("commission", "REAL NOT NULL DEFAULT 0"),
        ("slippage", "REAL NOT NULL DEFAULT 0"),
        ("filled_qty", "REAL"),
    ):
        if name not in order_columns:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {name} {ddl}")
    conn.execute(
        "INSERT OR IGNORE INTO execution_settings(account)"
        " SELECT name FROM accounts"
    )


def finalize_migration(conn):
    """Backfill normalized opening balances and install immutable guards."""
    for (account,) in conn.execute("SELECT name FROM accounts").fetchall():
        ensure_opening_ledger(conn, account)
    install_guards(conn)


def migrate(conn):
    prepare_migration(conn)
    finalize_migration(conn)


def install_guards(conn):
    guards = {
        "ledger_transactions_update": (
            "BEFORE UPDATE ON ledger_transactions",
            "ledger transactions are append-only",
        ),
        "ledger_transactions_delete": (
            "BEFORE DELETE ON ledger_transactions",
            "ledger transactions are append-only",
        ),
        "ledger_entries_update": (
            "BEFORE UPDATE ON ledger_entries",
            "ledger entries are append-only",
        ),
        "ledger_entries_delete": (
            "BEFORE DELETE ON ledger_entries",
            "ledger entries are append-only",
        ),
    }
    for name, (event, message) in guards.items():
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS guard_{name}
            {event}
            BEGIN SELECT RAISE(ABORT, '{message}'); END
            """
        )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS guard_ledger_entry_insert
        BEFORE INSERT ON ledger_entries
        WHEN NEW.book IS NULL OR trim(NEW.book)=''
          OR NEW.amount IS NULL
          OR abs(NEW.amount*100-round(NEW.amount*100))>0.000001
          OR NOT EXISTS(
            SELECT 1 FROM ledger_transactions WHERE id=NEW.transaction_id)
        BEGIN SELECT RAISE(ABORT, 'invalid ledger entry'); END
        """
    )
    for operation in ("INSERT", "UPDATE"):
        conn.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS guard_execution_settings_values_{operation.lower()}
            BEFORE {operation} ON execution_settings
            WHEN NEW.commission_bps<0 OR NEW.commission_bps>10000
              OR NEW.slippage_bps<0 OR NEW.slippage_bps>10000
              OR NEW.liquidity_fraction<=0 OR NEW.liquidity_fraction>1
              OR (NEW.max_fill_quantity IS NOT NULL AND NEW.max_fill_quantity<=0)
            BEGIN SELECT RAISE(ABORT, 'invalid execution settings'); END
            """
        )


def post_ledger(
    conn,
    account,
    kind,
    entries,
    *,
    reference_type=None,
    reference_id=None,
    description=None,
    metadata=None,
):
    normalized = []
    totals = {}
    for entry in entries:
        commodity = entry.get("commodity", "USD")
        amount = _money(entry["amount"])
        normalized.append(
            (
                entry["book"],
                amount,
                commodity,
                entry.get("units"),
                entry.get("price"),
            )
        )
        totals[commodity] = _money(totals.get(commodity, 0) + amount)
    unbalanced = {key: value for key, value in totals.items() if value != 0}
    if unbalanced:
        raise RuntimeError(f"unbalanced ledger transaction: {unbalanced}")
    cur = conn.execute(
        "INSERT INTO ledger_transactions(account,ts,kind,reference_type,"
        "reference_id,description,metadata) VALUES(?,?,?,?,?,?,?)",
        (
            account,
            utcnow(),
            kind,
            reference_type,
            str(reference_id) if reference_id is not None else None,
            description,
            json.dumps(metadata or {}, sort_keys=True, separators=(",", ":")),
        ),
    )
    transaction_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO ledger_entries(transaction_id,book,amount,commodity,units,price)"
        " VALUES(?,?,?,?,?,?)",
        [(transaction_id, *entry) for entry in normalized],
    )
    return transaction_id


def _position_basis(conn, account):
    total = 0.0
    for qty, avg, mult, asset_class, margin in conn.execute(
        "SELECT qty,avg_cost,mult,asset_class,margin FROM positions WHERE account=?",
        (account,),
    ):
        total += margin if asset_class == "future" else qty * avg * mult
    return _money(total)


def ensure_opening_ledger(conn, account):
    if conn.execute(
        "SELECT 1 FROM ledger_transactions WHERE account=? LIMIT 1", (account,)
    ).fetchone():
        return None
    row = conn.execute("SELECT cash FROM accounts WHERE name=?", (account,)).fetchone()
    if not row:
        return None
    cash = _money(row[0])
    basis = _position_basis(conn, account)
    return post_ledger(
        conn,
        account,
        "opening",
        (
            {"book": "asset:cash", "amount": cash},
            {"book": "asset:position", "amount": basis},
            {"book": "equity:opening", "amount": -(cash + basis)},
        ),
        description="Schema-v5 opening balance",
    )


def record_cash(conn, account, amount, kind="cash.adjust"):
    amount = _money(amount)
    if not amount:
        return None
    return post_ledger(
        conn,
        account,
        kind,
        (
            {"book": "asset:cash", "amount": amount},
            {"book": "equity:contributions", "amount": -amount},
        ),
    )


def record_fill(conn, account, symbol, side, qty, price, before, after, commission=0):
    cash_delta = _money(after["cash"] - before["cash"])
    old = before["pos"].get(symbol)
    new = after["pos"].get(symbol)

    def basis(position):
        if not position:
            return 0.0
        if position["ac"] == "future":
            return position["margin"]
        return position["qty"] * position["avg"] * position["mult"]

    position_delta = _money(basis(new) - basis(old))
    commission = _money(commission)
    balancing = _money(-(cash_delta + position_delta + commission))
    return post_ledger(
        conn,
        account,
        "fill",
        (
            {"book": "asset:cash", "amount": cash_delta},
            {
                "book": "asset:position",
                "amount": position_delta,
                "units": qty if side == "buy" else -qty,
                "price": price,
            },
            {"book": "expense:commission", "amount": commission},
            {"book": "income:realized", "amount": balancing},
        ),
        reference_type="symbol",
        reference_id=symbol,
        metadata={"side": side, "quantity": qty, "price": price},
    )


def ledger_balance(conn, account):
    rows = conn.execute(
        "SELECT e.book,round(sum(e.amount),2)"
        " FROM ledger_entries e JOIN ledger_transactions t"
        " ON t.id=e.transaction_id WHERE t.account=? GROUP BY e.book ORDER BY e.book",
        (account,),
    ).fetchall()
    return {book: amount for book, amount in rows}


def reconcile(conn, account, repair=False):
    ensure_opening_ledger(conn, account)
    row = conn.execute("SELECT cash FROM accounts WHERE name=?", (account,)).fetchone()
    if not row:
        raise SystemExit(f"no account '{account}'")
    actual = {"asset:cash": _money(row[0]), "asset:position": _position_basis(conn, account)}
    books = ledger_balance(conn, account)
    differences = {
        book: _money(value - books.get(book, 0)) for book, value in actual.items()
    }
    repaired = False
    if repair and any(differences.values()):
        amount = _money(sum(differences.values()))
        post_ledger(
            conn,
            account,
            "reconciliation",
            (
                {"book": "asset:cash", "amount": differences["asset:cash"]},
                {"book": "asset:position", "amount": differences["asset:position"]},
                {"book": "equity:reconciliation", "amount": -amount},
            ),
            description="Reconciled ledger to portfolio state",
        )
        repaired = True
    return {
        "account": account,
        "actual": actual,
        "ledger": {key: books.get(key, 0) for key in actual},
        "differences": differences,
        "balanced": not any(differences.values()),
        "repaired": repaired,
    }


def close_ledger_account(conn, account):
    books = ledger_balance(conn, account)
    cash = _money(books.get("asset:cash", 0))
    position = _money(books.get("asset:position", 0))
    if not cash and not position:
        return None
    return post_ledger(
        conn,
        account,
        "account.close",
        (
            {"book": "asset:cash", "amount": -cash},
            {"book": "asset:position", "amount": -position},
            {"book": "equity:closure", "amount": cash + position},
        ),
        description="Closed operational ledger balances",
    )


def execution_settings(conn, account):
    conn.execute(
        "INSERT OR IGNORE INTO execution_settings(account) VALUES(?)", (account,)
    )
    row = conn.execute(
        "SELECT commission_bps,slippage_bps,max_fill_quantity,liquidity_fraction"
        " FROM execution_settings WHERE account=?",
        (account,),
    ).fetchone()
    return {
        "commission_bps": row[0],
        "slippage_bps": row[1],
        "max_fill_quantity": row[2],
        "liquidity_fraction": row[3],
    }


def set_execution_settings(
    conn,
    account,
    commission_bps=None,
    slippage_bps=None,
    max_fill_quantity=None,
    liquidity_fraction=None,
    clear_max_fill=False,
):
    current = execution_settings(conn, account)
    updated = {
        "commission_bps": current["commission_bps"]
        if commission_bps is None
        else float(commission_bps),
        "slippage_bps": current["slippage_bps"]
        if slippage_bps is None
        else float(slippage_bps),
        "max_fill_quantity": None
        if clear_max_fill
        else current["max_fill_quantity"]
        if max_fill_quantity is None
        else float(max_fill_quantity),
        "liquidity_fraction": current["liquidity_fraction"]
        if liquidity_fraction is None
        else float(liquidity_fraction),
    }
    values = list(updated.values())
    if not all(value is None or math.isfinite(value) for value in values):
        raise SystemExit("execution settings must be finite")
    if (
        updated["commission_bps"] < 0
        or updated["commission_bps"] > 10_000
        or updated["slippage_bps"] < 0
        or updated["slippage_bps"] > 10_000
        or not 0 < updated["liquidity_fraction"] <= 1
        or (
            updated["max_fill_quantity"] is not None
            and updated["max_fill_quantity"] <= 0
        )
    ):
        raise SystemExit("invalid execution settings")
    conn.execute(
        "INSERT OR REPLACE INTO execution_settings"
        "(account,commission_bps,slippage_bps,max_fill_quantity,liquidity_fraction)"
        " VALUES(?,?,?,?,?)",
        (account, *updated.values()),
    )
    return updated


def realistic_fill(conn, account, side, qty, price):
    settings = execution_settings(conn, account)
    fill_qty = qty * settings["liquidity_fraction"]
    if settings["max_fill_quantity"] is not None:
        fill_qty = min(fill_qty, settings["max_fill_quantity"])
    fill_qty = round(fill_qty, 8)
    direction = 1 if side == "buy" else -1
    fill_price = round(price * (1 + direction * settings["slippage_bps"] / 10_000), 6)
    commission = _money(
        fill_qty * fill_price * settings["commission_bps"] / 10_000
    )
    return {
        "requested_quantity": qty,
        "filled_quantity": fill_qty,
        "remaining_quantity": round(qty - fill_qty, 8),
        "fill_price": fill_price,
        "slippage": _money(abs(fill_price - price) * fill_qty),
        "commission": commission,
        "partial": fill_qty + 1e-9 < qty,
    }


def journal_add(
    conn, account, title, body="", tags=None, symbol=None, order_id=None, attachment=None
):
    title = title.strip()
    if not title:
        raise SystemExit("journal title is required")
    tag_list = sorted({tag.strip() for tag in (tags or []) if tag.strip()})
    attachment_hash = None
    if attachment:
        path = Path(attachment).expanduser().resolve()
        if not path.is_file():
            raise SystemExit("journal attachment does not exist")
        attachment = str(path)
        attachment_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    cur = conn.execute(
        "INSERT INTO journal_entries(account,ts,title,body,tags,symbol,order_id,"
        "attachment,attachment_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            account,
            utcnow(),
            title,
            body,
            json.dumps(tag_list),
            symbol.upper() if symbol else None,
            order_id,
            attachment,
            attachment_hash,
        ),
    )
    return cur.lastrowid


def journal_list(conn, account, limit=100, tag=None, symbol=None):
    where = ["account=?"]
    params = [account]
    if symbol:
        where.append("symbol=?")
        params.append(symbol.upper())
    rows = conn.execute(
        "SELECT id,ts,title,body,tags,symbol,order_id,attachment,attachment_sha256"
        f" FROM journal_entries WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
        (*params, max(1, min(int(limit), 1000))),
    ).fetchall()
    result = [
        {
            "id": row[0],
            "timestamp": row[1],
            "title": row[2],
            "body": row[3],
            "tags": json.loads(row[4]),
            "symbol": row[5],
            "order_id": row[6],
            "attachment": row[7],
            "attachment_sha256": row[8],
        }
        for row in rows
    ]
    return [entry for entry in result if not tag or tag in entry["tags"]]


def journal_delete(conn, account, entry_id):
    cur = conn.execute(
        "DELETE FROM journal_entries WHERE id=? AND account=?", (entry_id, account)
    )
    if not cur.rowcount:
        raise SystemExit(f"no journal entry #{entry_id}")


def performance_attribution(conn, account):
    rows = conn.execute(
        "SELECT t.reference_id,"
        " round(-sum(CASE WHEN e.book='income:realized' THEN e.amount ELSE 0 END),2),"
        " round(sum(CASE WHEN e.book='expense:commission' THEN e.amount ELSE 0 END),2),"
        " COUNT(DISTINCT t.id)"
        " FROM ledger_transactions t JOIN ledger_entries e"
        " ON e.transaction_id=t.id"
        " WHERE t.account=? AND t.kind='fill' AND t.reference_id IS NOT NULL"
        " GROUP BY t.reference_id ORDER BY 2 DESC",
        (account,),
    ).fetchall()
    return [
        {
            "symbol": symbol,
            "realized_before_commission": realized,
            "commission": commission,
            "net_realized": round(realized - commission, 2),
            "fills": fills,
        }
        for symbol, realized, commission, fills in rows
    ]


def schedule_add(conn, name, action, interval_seconds, payload=None):
    allowed = {"tick", "backup", "reconcile"}
    if action not in allowed:
        raise SystemExit(f"automation action must be one of: {', '.join(sorted(allowed))}")
    if interval_seconds < 60:
        raise SystemExit("automation interval must be at least 60 seconds")
    now = datetime.now(timezone.utc)
    conn.execute(
        "INSERT INTO automation_jobs(name,action,payload,interval_seconds,next_run,created)"
        " VALUES(?,?,?,?,?,?)",
        (
            name,
            action,
            json.dumps(payload or {}, sort_keys=True),
            interval_seconds,
            (now + timedelta(seconds=interval_seconds)).isoformat(timespec="seconds"),
            now.isoformat(timespec="seconds"),
        ),
    )


def schedule_list(conn):
    return [
        {
            "id": row[0],
            "name": row[1],
            "action": row[2],
            "payload": json.loads(row[3]),
            "interval_seconds": row[4],
            "next_run": row[5],
            "enabled": bool(row[6]),
        }
        for row in conn.execute(
            "SELECT id,name,action,payload,interval_seconds,next_run,enabled"
            " FROM automation_jobs ORDER BY id"
        )
    ]


def run_due(conn, executor, now=None):
    now = now or datetime.now(timezone.utc)
    due = conn.execute(
        "SELECT id,name,action,payload,interval_seconds FROM automation_jobs"
        " WHERE enabled=1 AND next_run<=? ORDER BY next_run,id",
        (now.isoformat(timespec="seconds"),),
    ).fetchall()
    results = []
    for job_id, name, action, payload, interval in due:
        started = utcnow()
        run = conn.execute(
            "INSERT INTO automation_runs(job_id,started,status) VALUES(?,?,'running')",
            (job_id, started),
        ).lastrowid
        try:
            output = executor(action, json.loads(payload))
            status, error = "ok", None
        except (Exception, SystemExit) as exc:
            output, status, error = None, "failed", str(exc)
        conn.execute(
            "UPDATE automation_runs SET finished=?,status=?,output=?,error=? WHERE id=?",
            (utcnow(), status, json.dumps(output), error, run),
        )
        conn.execute(
            "UPDATE automation_jobs SET next_run=? WHERE id=?",
            (
                (now + timedelta(seconds=interval)).isoformat(timespec="seconds"),
                job_id,
            ),
        )
        results.append({"job": name, "status": status, "output": output, "error": error})
    return results


def backup_inventory(database_path, manual_directory=None):
    database = Path(database_path).expanduser().resolve()
    directories = [
        Path(manual_directory or "~/.papertrade_backups").expanduser(),
        Path(f"{database}.migrations"),
    ]
    rows = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.db"), reverse=True):
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
                integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                conn.close()
            except sqlite3.Error as exc:
                integrity, version = f"error: {exc}", None
            rows.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "modified": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat(timespec="seconds"),
                    "schema_version": version,
                    "integrity": integrity,
                }
            )
    return rows


def prune_backups(database_path, keep=10, manual_directory=None):
    if keep < 1:
        raise SystemExit("backup retention must keep at least one backup")
    inventory = backup_inventory(database_path, manual_directory)
    manual = Path(manual_directory or "~/.papertrade_backups").expanduser().resolve()
    candidates = [
        row for row in inventory if Path(row["path"]).resolve().parent == manual
    ]
    removed = []
    for row in sorted(candidates, key=lambda item: item["modified"], reverse=True)[keep:]:
        path = Path(row["path"])
        path.unlink()
        for suffix in ("-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        removed.append(row["name"])
    return removed


def restore_backup(database_path, backup_path, expected_schema):
    database = Path(database_path).expanduser().resolve()
    backup = Path(backup_path).expanduser().resolve()
    if not backup.is_file():
        raise SystemExit("backup file does not exist")
    source = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        if source.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SystemExit("backup failed SQLite integrity validation")
        version = source.execute("PRAGMA user_version").fetchone()[0]
        if version > expected_schema:
            raise SystemExit("backup schema is newer than this TradingCLI release")
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{database.name}.restore-", suffix=".db", dir=database.parent
        )
        os.close(descriptor)
        target = sqlite3.connect(temporary)
        try:
            source.backup(target)
        finally:
            target.close()
        os.chmod(temporary, 0o600)
        shutil.copy2(database, f"{database}.pre-restore")
        os.chmod(f"{database}.pre-restore", 0o600)
        os.replace(temporary, database)
        for suffix in ("-wal", "-shm"):
            try:
                Path(f"{database}{suffix}").unlink()
            except FileNotFoundError:
                pass
    finally:
        source.close()
    return {"restored": backup.name, "schema_version": version}


def encrypt_database_copy(database_path, output_path, password):
    """Create an authenticated encrypted snapshot; the live SQLite DB stays usable."""
    if not password:
        raise SystemExit("encryption password is required")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    database = Path(database_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    if output.exists():
        raise SystemExit("encrypted output already exists")
    snapshot = sqlite3.connect(database)
    descriptor, temporary = tempfile.mkstemp(suffix=".db")
    os.close(descriptor)
    target = sqlite3.connect(temporary)
    try:
        snapshot.backup(target)
    finally:
        target.close()
        snapshot.close()
    try:
        plaintext = Path(temporary).read_bytes()
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = hashlib.scrypt(
            password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32
        )
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, b"tradingcli-db-v1")
        output.write_bytes(b"TRADINGCLI-ENC-1" + salt + nonce + ciphertext)
        os.chmod(output, 0o600)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {"path": str(output), "bytes": output.stat().st_size}


def decrypt_database_copy(encrypted_path, output_path, password):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    source = Path(encrypted_path).expanduser().resolve().read_bytes()
    if not source.startswith(b"TRADINGCLI-ENC-1"):
        raise SystemExit("not a TradingCLI encrypted database")
    offset = len(b"TRADINGCLI-ENC-1")
    salt, nonce, ciphertext = source[offset : offset + 16], source[offset + 16 : offset + 28], source[offset + 28 :]
    key = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, b"tradingcli-db-v1")
    except Exception:
        raise SystemExit("decryption failed") from None
    output = Path(output_path).expanduser().resolve()
    if output.exists():
        raise SystemExit("decryption output already exists")
    output.write_bytes(plaintext)
    os.chmod(output, 0o600)
    conn = sqlite3.connect(f"file:{output}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            output.unlink(missing_ok=True)
            raise SystemExit("decrypted database failed integrity validation")
    finally:
        conn.close()
    return {"path": str(output), "bytes": output.stat().st_size}


def broker_export(conn, account, broker="generic"):
    headers = {
        "generic": ["timestamp", "symbol", "side", "quantity", "price", "commission"],
        "alpaca": ["filled_at", "symbol", "side", "filled_qty", "filled_avg_price", "commission"],
        "ibkr": ["DateTime", "Symbol", "Buy/Sell", "Quantity", "T. Price", "Comm/Fee"],
    }
    if broker not in headers:
        raise SystemExit("broker must be generic, alpaca, or ibkr")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(headers[broker])
    for row in conn.execute(
        "SELECT ts,symbol,side,qty,filled_price,commission FROM orders"
        " WHERE account=? AND status IN ('filled','settled','exercised') ORDER BY id",
        (account,),
    ):
        writer.writerow(row)
    return output.getvalue()


def broker_import(conn, account, content, broker, fill_callback, source_name=None):
    mappings = {
        "generic": ("timestamp", "symbol", "side", "quantity", "price", "commission"),
        "alpaca": ("filled_at", "symbol", "side", "filled_qty", "filled_avg_price", "commission"),
        "ibkr": ("DateTime", "Symbol", "Buy/Sell", "Quantity", "T. Price", "Comm/Fee"),
    }
    if broker not in mappings:
        raise SystemExit("broker must be generic, alpaca, or ibkr")
    fingerprint = hashlib.sha256((broker + "\0" + content).encode()).hexdigest()
    existing = conn.execute(
        "SELECT rows_imported FROM broker_imports WHERE fingerprint=?", (fingerprint,)
    ).fetchone()
    if existing:
        return {"imported": 0, "duplicate": True, "previous_rows": existing[0]}
    names = mappings[broker]
    rows = list(csv.DictReader(io.StringIO(content)))
    imported = 0
    for row in rows:
        timestamp, symbol, side, quantity, price, commission = (
            row.get(name, "") for name in names
        )
        normalized_side = side.strip().lower()
        if normalized_side in {"bot", "buy"}:
            normalized_side = "buy"
        elif normalized_side in {"sld", "sell"}:
            normalized_side = "sell"
        else:
            raise SystemExit(f"unsupported broker side: {side}")
        fill_callback(
            account,
            symbol.strip().upper(),
            normalized_side,
            float(quantity),
            float(price),
            timestamp,
            float(commission or 0),
        )
        imported += 1
    conn.execute(
        "INSERT INTO broker_imports VALUES(?,?,?,?,?,?)",
        (fingerprint, broker, utcnow(), account, imported, source_name),
    )
    return {"imported": imported, "duplicate": False, "fingerprint": fingerprint}


def walk_forward_sma(symbol, prices=None, short_windows=(5, 10, 20), long_windows=(50, 100, 200)):
    if prices is None:
        import yfinance as yf

        frame = yf.download(symbol, period="10y", auto_adjust=True, progress=False)
        series = frame["Close"]
        if getattr(series, "ndim", 1) > 1:
            series = series.iloc[:, 0]
        prices = [float(value) for value in series.dropna()]
    prices = [float(value) for value in prices if math.isfinite(float(value)) and float(value) > 0]
    if len(prices) < max(long_windows) + 30:
        raise SystemExit("not enough price history for walk-forward analysis")
    split = int(len(prices) * 0.7)

    def score(values, short, long):
        returns = []
        for index in range(long, len(values) - 1):
            short_avg = sum(values[index - short : index]) / short
            long_avg = sum(values[index - long : index]) / long
            exposure = 1 if short_avg > long_avg else 0
            returns.append(exposure * (values[index + 1] / values[index] - 1))
        if not returns:
            return -math.inf
        mean = sum(returns) / len(returns)
        variance = sum((value - mean) ** 2 for value in returns) / max(1, len(returns) - 1)
        return mean / math.sqrt(variance) * math.sqrt(252) if variance > 0 else 0

    candidates = [
        (score(prices[:split], short, long), short, long)
        for short in short_windows
        for long in long_windows
        if short < long
    ]
    training_score, short, long = max(candidates)
    test_score = score(prices[split - long :], short, long)
    return {
        "symbol": symbol.upper(),
        "observations": len(prices),
        "training_observations": split,
        "test_observations": len(prices) - split,
        "selected": {"short_window": short, "long_window": long},
        "training_sharpe": training_score,
        "out_of_sample_sharpe": test_score,
        "method": "70/30 chronological split; long-or-cash SMA crossover",
    }


def serve_api(database_factory, host, port, token, allow_mutations=False):
    if not token or len(token) < 24:
        raise SystemExit("API token must contain at least 24 characters")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("remote API binds to loopback only")

    class Handler(BaseHTTPRequestHandler):
        server_version = "TradingCLI/0.4"

        def _json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            supplied = self.headers.get("Authorization", "")
            return hmac.compare_digest(supplied, f"Bearer {token}")

        def do_GET(self):
            if not self._authorized():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            parsed = urlparse(self.path)
            conn = database_factory()
            try:
                import papertrade as pt

                if parsed.path == "/health":
                    data = pt.healthcheck(conn)
                elif parsed.path == "/accounts":
                    data = [
                        {"name": row[0], "cash": row[1]}
                        for row in conn.execute(
                            "SELECT name,cash FROM accounts ORDER BY name"
                        )
                    ]
                elif parsed.path == "/positions":
                    account = parse_qs(parsed.query).get("account", [None])[0]
                    if not account:
                        raise SystemExit("account query parameter is required")
                    data = pt.list_positions(conn, account)
                elif parsed.path == "/journal":
                    account = parse_qs(parsed.query).get("account", [None])[0]
                    if not account:
                        raise SystemExit("account query parameter is required")
                    data = journal_list(conn, account)
                else:
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                self._json(200, {"ok": True, "data": data})
            except (SystemExit, ValueError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            finally:
                conn.close()

        def do_POST(self):
            if not self._authorized():
                self._json(401, {"ok": False, "error": "unauthorized"})
                return
            if not allow_mutations:
                self._json(403, {"ok": False, "error": "mutations disabled"})
                return
            declared = int(self.headers.get("Content-Length", "0"))
            if declared < 0 or declared > 1_000_000:
                self._json(413, {"ok": False, "error": "request body too large"})
                return
            length = declared
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "invalid JSON"})
                return
            conn = database_factory()
            try:
                import papertrade as pt

                if self.path == "/preview":
                    data = pt.preview_order(
                        conn,
                        payload["account"],
                        payload["symbol"],
                        payload["side"],
                        float(payload["quantity"]),
                        payload.get("price"),
                    )
                else:
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                self._json(200, {"ok": True, "data": data})
            except (KeyError, SystemExit, ValueError) as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            finally:
                conn.close()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"TradingCLI API listening on http://{host}:{server.server_port}")
    server.serve_forever()


_price_cache = {}


def provider_price(symbol, yahoo_fetch, ttl=5.0):
    """Price provider chain: optional static JSON, then Yahoo, with stale fallback."""
    now = time.monotonic()
    cached = _price_cache.get(symbol)
    if cached and cached["expires"] > now:
        return cached["price"]
    providers = [
        item.strip().lower()
        for item in os.environ.get("PAPERTRADE_PRICE_PROVIDERS", "static,yahoo").split(",")
        if item.strip()
    ]
    errors = []
    for provider in providers:
        try:
            if provider == "static":
                values = json.loads(os.environ.get("PAPERTRADE_STATIC_PRICES", "{}"))
                price = float(values[symbol])
            elif provider == "yahoo":
                price = float(yahoo_fetch())
            else:
                raise ValueError(f"unknown provider {provider}")
            if not math.isfinite(price) or price <= 0:
                raise ValueError("non-positive price")
            _price_cache[symbol] = {
                "price": price,
                "expires": now + ttl,
                "provider": provider,
            }
            return price
        except (Exception, SystemExit) as exc:
            errors.append(f"{provider}: {exc}")
    if cached:
        return cached["price"]
    raise SystemExit(f"all price providers failed ({'; '.join(errors)})")


def feature_health(conn):
    ledger_imbalance = conn.execute(
        "SELECT COUNT(*) FROM ("
        " SELECT e.transaction_id FROM ledger_entries e"
        " GROUP BY e.transaction_id HAVING abs(sum(e.amount))>0.000001)"
    ).fetchone()[0]
    failed_automations = conn.execute(
        "SELECT COUNT(*) FROM automation_runs WHERE status='failed'"
    ).fetchone()[0]
    ledger_orphans = conn.execute(
        "SELECT COUNT(*) FROM ledger_entries e LEFT JOIN ledger_transactions t"
        " ON t.id=e.transaction_id WHERE t.id IS NULL"
    ).fetchone()[0]
    invalid_execution = conn.execute(
        "SELECT COUNT(*) FROM execution_settings"
        " WHERE commission_bps<0 OR commission_bps>10000"
        " OR slippage_bps<0 OR slippage_bps>10000"
        " OR liquidity_fraction<=0 OR liquidity_fraction>1"
        " OR (max_fill_quantity IS NOT NULL AND max_fill_quantity<=0)"
    ).fetchone()[0]
    return {
        "ledger_balanced": ledger_imbalance == 0 and ledger_orphans == 0,
        "unbalanced_transactions": ledger_imbalance,
        "orphaned_ledger_entries": ledger_orphans,
        "invalid_execution_settings": invalid_execution,
        "valid": ledger_imbalance == 0
        and ledger_orphans == 0
        and invalid_execution == 0,
        "journal_entries": conn.execute(
            "SELECT COUNT(*) FROM journal_entries"
        ).fetchone()[0],
        "automations": conn.execute(
            "SELECT COUNT(*) FROM automation_jobs WHERE enabled=1"
        ).fetchone()[0],
        "failed_automation_runs": failed_automations,
    }
