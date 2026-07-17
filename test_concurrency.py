"""Multi-process SQLite regression checks. Run: python3 test_concurrency.py"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor


ROOT = Path(__file__).resolve().parent
WORKER = r"""
import sys
import papertrade as pt

action = sys.argv[1]
conn = pt.db()
try:
    if action == "buy":
        pt.place(conn, "race", "AAPL", "buy", 1, None, price_fn=lambda _s: 100.0)
    elif action == "buy_idem":
        pt.place(conn, "idem", "AAPL", "buy", 1, None,
                 price_fn=lambda _s: 100.0, source="codex", request_id="same-trade")
    elif action == "deposit":
        pt.adjust_cash(conn, "limit", 1.0)
    elif action == "deposit_idem":
        pt.adjust_cash(conn, "idem", 10.0, source="codex", request_id="same-deposit")
    elif action == "tick_limit":
        pt.tick(conn, price_fn=lambda _s: 100.0)
    elif action == "tick_expiry":
        pt.tick(conn, price_fn=lambda _s: 175.0)
    else:
        raise RuntimeError(action)
finally:
    conn.close()
"""


def run_many(action, count, env):
    def invoke(_):
        return subprocess.run(
            [sys.executable, "-c", WORKER, action],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(invoke, range(count)))


with tempfile.TemporaryDirectory() as tmp:
    db_path = str(Path(tmp) / "concurrency.db")
    os.environ["PAPERTRADE_DB"] = db_path
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    import papertrade as pt

    conn = pt.db()
    pt.create_account(conn, "race", 1_000_000)
    conn.close()

    # Simulates independent AI-agent MCP processes buying at once.
    run_many("buy", 75, env)
    conn = pt.db()
    qty = conn.execute(
        "SELECT qty FROM positions WHERE account='race' AND symbol='AAPL'"
    ).fetchone()[0]
    orders = conn.execute(
        "SELECT COUNT(*) FROM orders WHERE account='race' AND status='filled'"
    ).fetchone()[0]
    cash = conn.execute("SELECT cash FROM accounts WHERE name='race'").fetchone()[0]
    assert qty == 75, qty
    assert orders == 75, orders
    assert cash == 992_500, cash

    # MCP/client retry storms with one key produce one mutation and one audit event.
    pt.create_account(conn, "idem", 1_000, make_default=False)
    conn.close()
    run_many("buy_idem", 30, env)
    run_many("deposit_idem", 30, env)
    conn = pt.db()
    assert (
        conn.execute(
            "SELECT qty FROM positions WHERE account='idem' AND symbol='AAPL'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT cash FROM accounts WHERE name='idem'").fetchone()[0] == 910
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM orders WHERE account='idem'").fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM audit_log WHERE source='codex'").fetchone()[
            0
        ]
        == 2
    )

    # A crossed limit must fill once even when many tick workers see it pending.
    pt.create_account(conn, "limit", 10_000, make_default=False)
    pt.place(conn, "limit", "AAPL", "buy", 1, 150.0, price_fn=lambda _s: 100.0)
    conn.close()
    run_many("tick_limit", 20, env)
    conn = pt.db()
    assert (
        conn.execute(
            "SELECT qty FROM positions WHERE account='limit' AND symbol='AAPL'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM orders WHERE account='limit' AND status='filled'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT cash FROM accounts WHERE name='limit'").fetchone()[0]
        == 9_900
    )

    # Cash read-modify-write and its ledger event must commit atomically.
    conn.close()
    run_many("deposit", 40, env)
    conn = pt.db()
    cash, deposits = conn.execute(
        "SELECT cash,deposits FROM accounts WHERE name='limit'"
    ).fetchone()
    assert (cash, deposits) == (9_940, 10_040), (cash, deposits)
    assert (
        conn.execute("SELECT COUNT(*) FROM cashflow WHERE account='limit'").fetchone()[
            0
        ]
        == 41
    )

    # Expiry settlement also has to be exactly-once under concurrent ticks.
    expired = "AAPL200101C00150000"
    pt.create_account(conn, "expiry", 10_000, make_default=False)
    pt.place(conn, "expiry", expired, "buy", 1, None, price_fn=lambda _s: 20.0)
    conn.close()
    run_many("tick_expiry", 20, env)
    conn = pt.db()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM positions WHERE account='expiry'"
        ).fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM orders WHERE account='expiry' AND status='settled'"
        ).fetchone()[0]
        == 1
    )
    cash, realized = conn.execute(
        "SELECT cash,realized FROM accounts WHERE name='expiry'"
    ).fetchone()
    assert (cash, realized) == (10_500, 500), (cash, realized)
    conn.close()

print("concurrency checks passed")
