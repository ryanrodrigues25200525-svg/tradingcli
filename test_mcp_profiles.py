"""Isolated contract checks for the selectable MCP catalogs and envelopes."""

import json
import os
import subprocess
import sys


PROBE = r"""
import asyncio
import json

import mcp_server as server


async def invoke(name, arguments=None):
    return await server.mcp._tool_manager._tools[name].run(
        arguments or {}, convert_result=False
    )


names = sorted(server.mcp._tool_manager._tools)
print(json.dumps({
    "profile": server.ACTIVE_MCP_PROFILE,
    "response_format": server.ACTIVE_MCP_RESPONSE_FORMAT,
    "names": names,
    "health": asyncio.run(invoke("healthcheck")),
    "missing_order": asyncio.run(invoke("order_get", {"order_id": 999999})),
}))
"""


def probe(profile, response_format=None):
    env = os.environ.copy()
    env["PAPERTRADE_MCP_PROFILE"] = profile
    env["PAPERTRADE_DB"] = os.path.join(
        os.environ["PAPERTRADE_PROFILE_TEST_DIR"], f"{profile}.db"
    )
    if response_format is None:
        env.pop("PAPERTRADE_MCP_RESPONSE_FORMAT", None)
    else:
        env["PAPERTRADE_MCP_RESPONSE_FORMAT"] = response_format
    completed = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(completed.stdout)


def assert_success_envelope(raw):
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["data"]["status"] == "ok"


def assert_error_envelope(raw):
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "tool_error"
    assert "order not found" in payload["error"]["message"]


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        os.environ["PAPERTRADE_PROFILE_TEST_DIR"] = directory

        core = probe("core")
        core_names = set(core["names"])
        assert core["profile"] == "core"
        assert core["response_format"] == "json"
        assert len(core_names) == 63
        assert {
            "mcp_catalog",
            "order_submit",
            "order_cancel",
            "preview_order",
            "portfolio_backtest",
            "rebalance_suggest",
        } <= core_names
        assert (
            not {
                "buy",
                "cancel_order",
                "delete_account",
                "reset_account",
                "option_exercise",
            }
            & core_names
        )
        assert_success_envelope(core["health"])
        assert_error_envelope(core["missing_order"])

        advanced = probe("advanced")
        advanced_names = set(advanced["names"])
        assert advanced["profile"] == "advanced"
        assert advanced["response_format"] == "json"
        assert len(advanced_names) == 79
        assert {"delete_account", "reset_account", "option_exercise"} <= advanced_names
        assert (
            not {
                "buy",
                "sell",
                "cancel_order",
                "close_position",
                "quote",
                "trade_history",
                "watchlist",
            }
            & advanced_names
        )
        assert_success_envelope(advanced["health"])

        full = probe("full")
        full_names = set(full["names"])
        assert full["profile"] == "full"
        assert full["response_format"] == "legacy"
        assert len(full_names) == 86
        assert {"buy", "sell", "cancel_order", "trade_history"} <= full_names
        assert json.loads(full["health"])["status"] == "ok"

        compat = probe("compat")
        assert compat["profile"] == "full"
        assert compat["response_format"] == "legacy"
        assert compat["names"] == full["names"]

        legacy_core = probe("core", "legacy")
        assert legacy_core["response_format"] == "legacy"
        assert json.loads(legacy_core["health"])["status"] == "ok"

    print("MCP profile checks passed (63 core / 79 advanced / 86 full)")
