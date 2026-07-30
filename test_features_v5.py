"""Cross-feature contracts for the TradingCLI 0.4 operational platform."""

from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)
database = root / "features.db"
os.environ["PAPERTRADE_DB"] = str(database)

import papertrade as pt  # noqa: E402
import tradingcli_features as features  # noqa: E402


conn = pt.db()
with redirect_stdout(io.StringIO()):
    pt.create_account(conn, "platform", 10_000)

# Double-entry opening/funding/fill ledger and reconciliation.
assert features.feature_health(conn)["ledger_balanced"]
with redirect_stdout(io.StringIO()):
    pt.adjust_cash(conn, "platform", 250)
assert features.reconcile(conn, "platform")["balanced"]
opening_count = conn.execute(
    "SELECT COUNT(*) FROM ledger_transactions WHERE account='platform'"
).fetchone()[0]
try:
    conn.execute("UPDATE ledger_entries SET amount=0")
except sqlite3.IntegrityError:
    pass
else:
    raise AssertionError("ledger entries must be append-only")

# Realistic commission, slippage, and deterministic partial fills.
with pt.writing(conn):
    settings = features.set_execution_settings(
        conn,
        "platform",
        commission_bps=5,
        slippage_bps=10,
        max_fill_quantity=2,
        liquidity_fraction=0.5,
    )
assert settings["max_fill_quantity"] == 2
execution = features.realistic_fill(conn, "platform", "buy", 10, 100)
assert execution == {
    "requested_quantity": 10,
    "filled_quantity": 2.0,
    "remaining_quantity": 8.0,
    "fill_price": 100.1,
    "slippage": 0.2,
    "commission": 0.1,
    "partial": True,
}
with redirect_stdout(io.StringIO()):
    order_id = pt.place(
        conn, "platform", "AAPL", "buy", 10, None, price_fn=lambda _symbol: 100
    )
assert conn.execute(
    "SELECT status,filled_qty,commission,slippage FROM orders WHERE id=?",
    (order_id,),
).fetchone() == ("partially_filled", 2.0, 0.1, 0.2)
assert conn.execute(
    "SELECT qty FROM positions WHERE account='platform' AND symbol='AAPL'"
).fetchone()[0] == 2
assert features.reconcile(conn, "platform")["balanced"]

# Expanded exposure, concentration, loss, and drawdown limits.
with redirect_stdout(io.StringIO()):
    pt.create_account(conn, "risk", 10_000, make_default=False)
pt.set_risk_limits(
    conn,
    "risk",
    max_daily_loss=50,
    max_drawdown=0.1,
    max_symbol_exposure=100,
    max_concentration=0.5,
)
preview = pt.preview_order(
    conn, "risk", "MSFT", "buy", 2, 100, price_fn=lambda _symbol: 100
)
assert not preview["allowed"] and "symbol exposure" in preview["reason"]

# Deleted account names can be reused because their ledger assets close to zero.
with redirect_stdout(io.StringIO()):
    pt.create_account(conn, "recycled", 500, make_default=False)
    pt.wipe_account(conn, "recycled")
    pt.create_account(conn, "recycled", 750, make_default=False)
assert features.reconcile(conn, "recycled")["balanced"]

# Journal tags and attachment integrity.
attachment = root / "chart.txt"
attachment.write_text("simulated chart", encoding="utf-8")
with pt.writing(conn):
    journal_id = features.journal_add(
        conn,
        "platform",
        "Partial fill review",
        "Liquidity cap behaved as configured.",
        ["execution", "review"],
        "AAPL",
        order_id,
        attachment,
    )
journal = features.journal_list(conn, "platform", tag="execution")
assert journal[0]["id"] == journal_id
assert journal[0]["attachment_sha256"]
assert features.performance_attribution(conn, "platform")[0]["symbol"] == "AAPL"

# Persisted schedules and run history.
with pt.writing(conn):
    features.schedule_add(
        conn, "daily-reconcile", "reconcile", 60, {"account": "platform"}
    )
    conn.execute(
        "UPDATE automation_jobs SET next_run=? WHERE name='daily-reconcile'",
        ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
    )
runs = features.run_due(
    conn,
    lambda action, payload: features.reconcile(
        conn, payload["account"], repair=action == "reconcile"
    ),
)
assert runs[0]["status"] == "ok"
assert conn.execute(
    "SELECT status FROM automation_runs ORDER BY id DESC LIMIT 1"
).fetchone()[0] == "ok"

# Provider chain, cache, and deterministic static fallback.
features._price_cache.clear()
os.environ["PAPERTRADE_STATIC_PRICES"] = '{"NVDA": 123.45}'
assert pt.live_price("NVDA") == 123.45
del os.environ["PAPERTRADE_STATIC_PRICES"]
assert pt.live_price("NVDA") == 123.45  # cached, without a network call

# Walk-forward selection reports a separate chronological test window.
prices = [100 + index * 0.2 + (index % 7) * 0.03 for index in range(360)]
walk_forward = features.walk_forward_sma("SYNTH", prices)
assert walk_forward["training_observations"] < walk_forward["observations"]
assert walk_forward["test_observations"] > 0

# Broker CSV export/import and duplicate protection.
exported = features.broker_export(conn, "platform", "generic")
assert "timestamp,symbol,side,quantity,price,commission" in exported
with redirect_stdout(io.StringIO()):
    pt.create_account(conn, "imported", 10_000, make_default=False)
content = (
    "timestamp,symbol,side,quantity,price,commission\n"
    "2026-01-01T10:00:00+00:00,AMD,buy,3,50,1.25\n"
)


def import_fill(account, symbol, side, quantity, price, timestamp, commission):
    with pt.writing(conn):
        pt._fill_locked(
            conn,
            account,
            symbol,
            side,
            quantity,
            price,
            enforce_risk=False,
        )
        order = pt._insert_order_locked(
            conn,
            account,
            symbol,
            side,
            quantity,
            None,
            "filled",
            price,
            timestamp,
            "import:test",
            None,
        )
        conn.execute(
            "UPDATE orders SET commission=? WHERE id=?", (pt._money(commission), order)
        )


imported = features.broker_import(
    conn, "imported", content, "generic", import_fill, "trades.csv"
)
assert imported["imported"] == 1
assert features.broker_import(
    conn, "imported", content, "generic", import_fill, "trades.csv"
)["duplicate"]

# Backup inventory, retention, restore, and authenticated encryption round-trip.
backup_directory = root / "backups"
backup = Path(pt.backup_database(conn, str(backup_directory)))
inventory = features.backup_inventory(database, str(backup_directory))
assert inventory[0]["integrity"] == "ok"
second_backup = Path(pt.backup_database(conn, str(backup_directory)))
assert second_backup.exists()
removed = features.prune_backups(database, 1, str(backup_directory))
assert len(removed) == 1
backup = next(backup_directory.glob("*.db"))

encrypted = root / "portfolio.db.enc"
decrypted = root / "portfolio-decrypted.db"
features.encrypt_database_copy(database, encrypted, "correct horse battery staple")
features.decrypt_database_copy(
    encrypted, decrypted, "correct horse battery staple"
)
check = sqlite3.connect(decrypted)
assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
check.close()

conn.close()
features.restore_backup(database, backup, pt.SCHEMA_VERSION)
pt.DB = str(database)
conn = pt.db()
assert pt.healthcheck(conn)["status"] == "ok"
assert conn.execute(
    "SELECT COUNT(*) FROM ledger_transactions WHERE account='platform'"
).fetchone()[0] >= opening_count
conn.close()

# Authenticated loopback API: unauthorized requests fail and authorized health works.
try:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
except PermissionError:
    port = None  # restricted sandboxes; exercised in CI and release verification
api_env = os.environ.copy()
api_env["PAPERTRADE_DB"] = str(database)
api_env["PAPERTRADE_API_TOKEN"] = "test-token-with-at-least-24-characters"
if port is not None:
    process = subprocess.Popen(
        [
            sys.executable,
            "papertrade.py",
            "serve",
            "--port",
            str(port),
        ],
        cwd=Path(__file__).parent,
        env=api_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(50):
            try:
                urlopen(f"http://127.0.0.1:{port}/health", timeout=0.1)
            except HTTPError as exc:
                if exc.code == 401:
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("loopback API did not start")
        request = Request(
            f"http://127.0.0.1:{port}/health",
            headers={"Authorization": f"Bearer {api_env['PAPERTRADE_API_TOKEN']}"},
        )
        payload = json.loads(urlopen(request, timeout=2).read())
        assert payload["ok"] and payload["data"]["status"] == "ok"
    finally:
        process.terminate()
        process.wait(timeout=5)

print("schema-v5 operational features passed")
