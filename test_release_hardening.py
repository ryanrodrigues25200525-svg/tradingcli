"""Release-hardening checks for privacy, health, and version metadata."""

import os
from pathlib import Path
import stat
import tempfile


temporary = tempfile.TemporaryDirectory()
database_path = Path(temporary.name) / "private.db"
os.environ["PAPERTRADE_DB"] = str(database_path)

import papertrade as pt  # noqa: E402


conn = pt.db()
assert stat.S_IMODE(database_path.stat().st_mode) == 0o600

healthy = pt.healthcheck(conn)
assert healthy["status"] == "ok"
assert healthy["database"] == "private.db"
assert str(database_path.parent) not in healthy["database"]

conn.execute("PRAGMA user_version=2")
degraded = pt.healthcheck(conn)
assert degraded["status"] == "degraded"
assert degraded["schema_version"] == 2
conn.execute(f"PRAGMA user_version={pt.SCHEMA_VERSION}")

backup_directory = Path(temporary.name) / "backups"
backup_path = Path(pt.backup_database(conn, str(backup_directory)))
assert stat.S_IMODE(backup_directory.stat().st_mode) == 0o700
assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
conn.close()

try:
    pt.main(["--version"])
except SystemExit as exc:
    assert exc.code == 0
else:
    raise AssertionError("--version should exit through argparse")

print("release hardening checks passed")
