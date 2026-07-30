"""CLI command-tree and structured-output smoke checks."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


root = Path(__file__).resolve().parent
database = tempfile.mktemp(suffix=".db")
env = {**os.environ, "PAPERTRADE_DB": database, "PYTHONDONTWRITEBYTECODE": "1"}


def run(*args, check=True):
    return subprocess.run(
        [sys.executable, "papertrade.py", *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


schema = json.loads(run("--schema").stdout)
assert {"order", "position", "option", "watchlist", "data"} <= set(schema)
assert "backtest" in schema["operations"]

created = json.loads(run("new", "cli", "--cash", "25000", "--json").stdout)
assert created["ok"] and "created 'cli'" in created["output"][0]
accounts = json.loads(run("accounts", "--json").stdout)
assert accounts == [{"name": "cli", "cash": 25000.0, "default": True}]
backtest = json.loads(run("backtest", "--json").stdout)
assert backtest["status"] == "no_positions" and backtest["account"] == "cli"

preview = json.loads(
    run(
        "order",
        "submit",
        "AAPL",
        "--side",
        "buy",
        "--qty",
        "2",
        "--type",
        "limit",
        "--limit-price",
        "90",
        "--dry-run",
        "--json",
    ).stdout
)
assert preview["dry_run"] and preview["allowed"]

submitted = run(
    "order",
    "submit",
    "AAPL",
    "--side",
    "buy",
    "--qty",
    "2",
    "--type",
    "stop-limit",
    "--stop-price",
    "105",
    "--limit-price",
    "106",
    "--client-order-id",
    "cli-order-1",
)
assert "pending" in submitted.stdout
order = json.loads(
    run(
        "order",
        "get",
        "--client-order-id",
        "cli-order-1",
        "--json",
    ).stdout
)
assert order["order_type"] == "stop_limit" and order["client_order_id"] == "cli-order-1"

# cancel-all defaults to the selected account; cross-account cancellation is explicit.
run("new", "other", "--cash", "25000")
run(
    "order",
    "submit",
    "AMZN",
    "--side",
    "buy",
    "--qty",
    "1",
    "--type",
    "limit",
    "--limit-price",
    "50",
    "--client-order-id",
    "other-order-1",
    "--account",
    "other",
)
run("use", "cli")
run("order", "cancel-all")
cli_order = json.loads(
    run("order", "get", "--client-order-id", "cli-order-1", "--json").stdout
)
other_orders = json.loads(
    run("order", "list", "--account", "other", "--status", "all", "--json").stdout
)
assert cli_order["status"] == "canceled"
assert other_orders[0]["status"] == "pending"
run("order", "cancel-all", "--all-accounts")
other_orders = json.loads(
    run("order", "list", "--account", "other", "--status", "all", "--json").stdout
)
assert other_orders[0]["status"] == "canceled"

run("watchlist", "create", "Tech", "--symbols", "AAPL,MSFT")
watchlist = json.loads(run("watchlist", "get", "Tech", "--json").stdout)
assert watchlist["symbols"] == ["AAPL", "MSFT"]
activity = json.loads(run("activity", "--json").stdout)
assert any(event["type"] == "order" for event in activity)

doctor = json.loads(run("doctor", "--json").stdout)
assert doctor["status"] == "ok" and doctor["schema_version"] == 5
calendar = json.loads(
    run("calendar", "--start", "2026-07-01", "--end", "2026-07-06", "--json").stdout
)
assert calendar
assert run("accounts", "--csv").stdout.startswith("name,cash,default\n")

failure = run("order", "get", "--order-id", "999", "--json", check=False)
assert failure.returncode == 1 and json.loads(failure.stderr)["ok"] is False

print("CLI checks passed")
