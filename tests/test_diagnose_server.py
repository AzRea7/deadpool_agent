"""The read-only diagnose MCP server, in-process and over real stdio."""

import json
import os
from pathlib import Path

import pytest

from fleet.agents.toolbox import InProcessToolBox, StdioToolBox
from fleet.mcp_servers.backend import FixtureBackend
from fleet.mcp_servers.diagnose_server import build_server

CASES = sorted(Path("evals/incidents").glob("*.json"))
EXPECTED_TOOLS = [
    "disk_usage_scan",
    "event_log_tail",
    "iis_log_summary",
    "iis_state",
    "metric_series",
    "recent_deploys",
]


def test_every_tool_is_read_only() -> None:
    import asyncio

    server = build_server(FixtureBackend(CASES[0]))
    tools = asyncio.run(server.list_tools())
    assert sorted(t.name for t in tools) == EXPECTED_TOOLS
    for t in tools:
        assert t.annotations is not None
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False


@pytest.mark.parametrize("case", CASES, ids=lambda p: p.stem)
def test_every_case_answers_every_tool(case: Path) -> None:
    incident = json.loads(case.read_text(encoding="utf-8"))["incident"]
    instance = incident["instance"]
    tb = InProcessToolBox(build_server(FixtureBackend(case)))
    try:
        calls = {
            "metric_series": {"instance": instance, "metric": "disk_used_pct", "hours": 6},
            "event_log_tail": {"instance": instance, "log": "System"},
            "iis_log_summary": {"instance": instance},
            "recent_deploys": {"hours": 72},
            "disk_usage_scan": {"instance": instance},
            "iis_state": {"instance": instance},
        }
        for name, args in calls.items():
            text, is_error = tb.call(name, args)
            assert not is_error, (name, text)
            json.loads(text)
    finally:
        tb.close()


def test_parameters_are_validated() -> None:
    tb = InProcessToolBox(build_server(FixtureBackend(CASES[0])))
    try:
        assert tb.call("iis_state", {"instance": "web; Remove-Item C:\\"})[1] is True
        assert tb.call("metric_series", {"instance": "fleet000001", "metric": "x"})[1] is True
        bad_hours = {"instance": "fleet000001", "log": "System", "hours": 9999}
        assert tb.call("event_log_tail", bad_hours)[1] is True
    finally:
        tb.close()


def test_stdio_round_trip() -> None:
    env = dict(os.environ, FLEET_DIAGNOSE_CASE=str(Path("evals/incidents/pool_crash.json")))
    tb = StdioToolBox("fleet.mcp_servers.diagnose_server", env=env)
    try:
        assert [s["name"] for s in tb.specs()] == EXPECTED_TOOLS
        text, is_error = tb.call("iis_state", {"instance": "fleet000003"})
        assert not is_error and json.loads(text)["app_pools"][0]["state"] == "Stopped"
    finally:
        tb.close()
