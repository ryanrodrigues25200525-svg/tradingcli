"""Non-interactive first-run dashboard smoke check."""

import os
import tempfile


os.environ["PAPERTRADE_DB"] = tempfile.mktemp(suffix=".db")

import dashboard


class FakeConsole:
    def __init__(self):
        self.answers = iter(["main", "25000", "conservative"])

    def clear(self):
        pass

    def print(self, *_args, **_kwargs):
        pass

    def input(self, *_args, **_kwargs):
        return next(self.answers)


dashboard.time.sleep = lambda _seconds: None
dashboard.first_run_setup(FakeConsole())
conn = dashboard.pt.db()
assert (
    conn.execute("SELECT cash FROM accounts WHERE name='main'").fetchone()[0] == 25_000
)
assert (
    conn.execute("SELECT value FROM config WHERE key='setup_done'").fetchone()[0] == "1"
)
limits = dashboard.pt.risk_limits(conn, "main")
assert not limits["allow_short"] and limits["max_gross_leverage"] == 1
assert limits["max_order_notional"] == 6_250
assert (
    conn.execute("SELECT COUNT(*) FROM audit_log WHERE source='dashboard'").fetchone()[
        0
    ]
    == 2
)
conn.close()

print("dashboard checks passed")
