"""Local-only quote, alert, report, completion, and benchmark checks."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile


tmp = tempfile.TemporaryDirectory()
database = str(Path(tmp.name) / "simulation.db")
os.environ["PAPERTRADE_DB"] = database

import papertrade as pt  # noqa: E402


conn = pt.db()
pt.create_account(conn, "local", 25_000)

price_file = Path(tmp.name) / "prices.json"
price_file.write_text(json.dumps({"AAPL": 125.5, "MSFT": 410}), encoding="utf-8")
os.environ["PAPERTRADE_PRICE_PROVIDERS"] = "file"
os.environ["PAPERTRADE_PRICE_FILE"] = str(price_file)
pt.features._price_cache.clear()

assert pt.live_price("AAPL") == 125.5
cached = pt.features.cached_prices(database, {"AAPL"})
assert cached["AAPL"]["price"] == 125.5
assert pt.features.cache_status(conn)["prices"] == 1

with pt.writing(conn):
    alert_id = pt.features.alert_add(
        conn, "breakout", "AAPL", "above", 120, cooldown_seconds=60
    )
assert pt.features.alert_list(conn)[0]["id"] == alert_id
now = datetime.now(timezone.utc)
with pt.writing(conn):
    first = pt.features.alert_check(conn, {"AAPL": 125.5}, now=now)
    second = pt.features.alert_check(
        conn, {"AAPL": 126}, now=now + timedelta(seconds=30)
    )
assert len(first) == 1 and second == []
assert pt.features.alert_events(conn)[0]["name"] == "breakout"
inbox = Path(tmp.name) / "notifications.jsonl"
assert pt.features.write_local_notifications(first, inbox) == str(inbox)
assert json.loads(inbox.read_text())["name"] == "breakout"

report = pt.simulation_report(conn, "local")
assert report["live_execution"] is False
assert report["ledger"]["balanced"]
report_path = Path(tmp.name) / "report.json"
assert pt.save_simulation_report(report, report_path) == str(report_path)
assert json.loads(report_path.read_text())["account"] == "local"

for shell in ("bash", "zsh", "fish"):
    script = pt.completion_script(shell)
    assert "tradingcli" in script and "quotes" in script

with pt.writing(conn):
    for action in ("quotes", "alerts", "report"):
        pt.features.schedule_add(conn, f"job-{action}", action, 60)
assert {job["action"] for job in pt.features.schedule_list(conn)} >= {
    "quotes",
    "alerts",
    "report",
}

benchmark = pt.terminal_benchmark(3)
assert benchmark["passes"]["startup"] and benchmark["passes"]["database"]

with pt.writing(conn):
    pt.features.alert_delete(conn, alert_id)
assert pt.features.alert_list(conn) == []

conn.close()
tmp.cleanup()
print("local simulation feature checks passed")
