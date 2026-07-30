"""Schema-v5 migration, precision, guard, and rollback contracts."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sqlite3
import stat
import tempfile


temporary = tempfile.TemporaryDirectory()
root = Path(temporary.name)
os.environ["PAPERTRADE_DB"] = str(root / "fresh.db")

import papertrade as pt  # noqa: E402


def legacy_database(path, *, corrupt=False):
    conn = sqlite3.connect(path)
    conn.executescript(pt.SCHEMA)
    conn.execute("PRAGMA user_version=3")
    conn.execute(
        "INSERT INTO accounts(name,cash,deposits,realized)"
        " VALUES('legacy',100.015,100.015,0.005)"
    )
    conn.execute(
        "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            "missing" if corrupt else "legacy",
            "AAPL",
            1.123456789,
            12.1234567,
            1.0,
            "spot",
            0.0,
        ),
    )
    conn.commit()
    conn.close()


def rejected(conn, sql, parameters=()):
    try:
        conn.execute(sql, parameters)
    except sqlite3.IntegrityError:
        return
    raise AssertionError(f"database accepted an invalid write: {sql}")


# Fresh databases enforce relationships, domains, and documented precision.
pt.DB = str(root / "fresh.db")
conn = pt.db()
assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
assert pt.healthcheck(conn)["status"] == "ok"
with redirect_stdout(io.StringIO()):
    pt.create_account(conn, "fixed", 100.015)
    for _ in range(100):
        pt.adjust_cash(conn, "fixed", 0.1)
        pt.adjust_cash(conn, "fixed", -0.1)
assert conn.execute(
    "SELECT cash,deposits,realized FROM accounts WHERE name='fixed'"
).fetchone() == (100.02, 100.02, 0.0)
rejected(
    conn,
    "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin)"
    " VALUES('missing','AAPL',1,10,1,'spot',0)",
)
rejected(
    conn,
    "INSERT INTO positions(account,symbol,qty,avg_cost,mult,asset_class,margin)"
    " VALUES('fixed','AAPL',1.000000001,10,1,'spot',0)",
)
rejected(
    conn,
    "INSERT INTO orders(account,symbol,side,qty,status,order_type,time_in_force,"
    " order_class,extended_hours,triggered)"
    " VALUES('fixed','AAPL','hold',1,'pending','market','gtc','simple',0,0)",
)
rejected(
    conn,
    "INSERT INTO cashflow(account,ts,amount) VALUES('fixed','now',0.001)",
)
rejected(conn, "DELETE FROM accounts WHERE name='fixed'")
rejected(conn, "UPDATE accounts SET name='renamed-outside-api' WHERE name='fixed'")
conn.close()

# A valid v3 database is backed up before deterministic half-even normalization.
migration_path = root / "migration.db"
legacy_database(migration_path)
pt.DB = str(migration_path)
conn = pt.db()
assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
assert conn.execute(
    "SELECT cash,deposits,realized FROM accounts WHERE name='legacy'"
).fetchone() == (100.02, 100.02, 0.0)
assert conn.execute(
    "SELECT qty,avg_cost FROM positions WHERE account='legacy'"
).fetchone() == (1.12345679, 12.123457)
backup_path = Path(
    conn.execute(
        "SELECT value FROM config WHERE key='last_migration_backup'"
    ).fetchone()[0]
)
assert backup_path.is_file()
assert stat.S_IMODE(backup_path.parent.stat().st_mode) == 0o700
assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
backup = sqlite3.connect(backup_path)
assert backup.execute("PRAGMA user_version").fetchone()[0] == 3
assert backup.execute(
    "SELECT cash FROM accounts WHERE name='legacy'"
).fetchone()[0] == 100.015
backup.close()
assert pt.healthcheck(conn)["logical_integrity"]["total"] == 0
conn.close()

# Invalid legacy relationships block migration, preserve v3 data, and retain a backup.
corrupt_path = root / "corrupt.db"
legacy_database(corrupt_path, corrupt=True)
pt.DB = str(corrupt_path)
try:
    pt.db()
except RuntimeError as exc:
    assert "logical violation" in str(exc)
else:
    raise AssertionError("corrupt legacy database should not migrate")
raw = sqlite3.connect(corrupt_path)
assert raw.execute("PRAGMA user_version").fetchone()[0] == 3
assert raw.execute(
    "SELECT cash FROM accounts WHERE name='legacy'"
).fetchone()[0] == 100.015
raw.close()
assert list(Path(f"{corrupt_path}.migrations").glob("*.db"))

# Opening a database created by a newer release is refused without mutation.
future_path = root / "future.db"
future = sqlite3.connect(future_path)
future.execute("CREATE TABLE sentinel(value TEXT)")
future.execute("INSERT INTO sentinel VALUES('preserve me')")
future.execute("PRAGMA user_version=99")
future.commit()
future.close()
pt.DB = str(future_path)
try:
    pt.db()
except RuntimeError as exc:
    assert "newer than supported" in str(exc)
else:
    raise AssertionError("future schemas must be refused")
future = sqlite3.connect(future_path)
assert future.execute("SELECT value FROM sentinel").fetchone()[0] == "preserve me"
assert future.execute("PRAGMA user_version").fetchone()[0] == 99
future.close()

print("database v5 contracts passed")
