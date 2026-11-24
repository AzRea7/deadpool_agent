"""The action MCP server re-checks everything itself: no shortcut around the gates."""

import json
from pathlib import Path
from typing import Any

from fleet.agents.toolbox import InProcessToolBox
from fleet.governance.audit import InMemoryAuditLog
from fleet.governance.executor import SimExecutor, host_from_case
from fleet.governance.service import ActionService
from fleet.mcp_servers.action_server import build_server


def toolbox(case: str) -> tuple[InProcessToolBox, SimExecutor, dict[str, Any]]:
    spec = json.loads(Path(f"evals/incidents/{case}.json").read_text(encoding="utf-8"))
    executor = SimExecutor([host_from_case(spec)])
    service = ActionService(executor, InMemoryAuditLog(), kill_switch=lambda: False)
    inc = spec["incident"]
    return InProcessToolBox(build_server(service)), executor, inc


def context(inc: dict[str, Any], runbook: str) -> dict[str, Any]:
    return {
        "incident_id": "i",
        "run_id": "r",
        "instance": inc["instance"],
        "platform": inc["os"],
        "scenario": inc["scenario"],
        "severity": inc["severity"],
        "runbook_id": runbook,
        "retrieved": [runbook],
    }


def plan_from(dry: dict[str, Any], runbook: str) -> dict[str, Any]:
    return {
        "instance": dry["instance"],
        "action": dry["action"],
        "params": dry["params"],
        "runbook_id": runbook,
        "plan_hash": dry["plan_hash"],
        "dry_run_at": dry["at"],
        "rationale": "test",
    }


def test_tools_and_annotations() -> None:
    tb, _, _ = toolbox("disk_iis_logs")
    try:
        assert [s["name"] for s in tb.specs()] == [
            "dry_run_action",
            "execute_action",
            "list_actions",
        ]
    finally:
        tb.close()


def test_tier0_executes_and_verifies_through_the_server() -> None:
    tb, executor, inc = toolbox("disk_iis_logs")
    try:
        ctx = context(inc, "RB-001")
        actions = json.loads(
            tb.call("list_actions", {"platform": "windows", "runbook_id": "RB-001"})[0]
        )
        assert {a["action"] for a in actions["actions"]} == {
            "delete_iis_logs_older_than",
            "clear_temp",
            "clear_wer_dumps",
        }
        dry = json.loads(
            tb.call(
                "dry_run_action",
                {"context": ctx, "action": "delete_iis_logs_older_than", "params": {"days": 7}},
            )[0]
        )
        out, is_error = tb.call(
            "execute_action", {"context": ctx, "plan": plan_from(dry, "RB-001")}
        )
        assert not is_error, out
        assert json.loads(out)["verification"]["recovered"] is True
        assert len(executor.applied) == 1
    finally:
        tb.close()


def test_tier2_without_approval_is_refused_by_the_server() -> None:
    tb, executor, inc = toolbox("disk_freb")
    try:
        ctx = context(inc, "RB-002")
        dry = json.loads(
            tb.call(
                "dry_run_action",
                {
                    "context": ctx,
                    "action": "delete_paths",
                    "params": {"paths": ["C:\\inetpub\\logs\\FailedReqLogFiles"]},
                },
            )[0]
        )
        out, is_error = tb.call(
            "execute_action", {"context": ctx, "plan": plan_from(dry, "RB-002")}
        )
        assert is_error and json.loads(out)["failed_gate"] == "approval"
        assert executor.applied == []
    finally:
        tb.close()
