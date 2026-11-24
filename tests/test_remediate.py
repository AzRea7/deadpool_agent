"""Full incident graph with a scripted model: diagnose -> plan -> gate -> approve -> act -> verify.

Phase 5 done-criteria: an unapproved Tier 2 action is impossible, and every action is audited
and verified.
"""

import json
import re
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fleet.agents.diagnose import ModelTurn, RunbookHit, run_config
from fleet.agents.remediate import build_incident_graph
from fleet.agents.toolbox import InProcessToolBox, ToolBox
from fleet.governance.audit import InMemoryAuditLog
from fleet.governance.executor import SimExecutor, host_from_case
from fleet.governance.service import ActionService
from fleet.mcp_servers.backend import FixtureBackend
from fleet.mcp_servers.diagnose_server import build_server

Turn = list[dict[str, Any]] | Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
DIAGNOSE_TOOLS = {
    "metric_series",
    "system_log_tail",
    "web_log_summary",
    "recent_deploys",
    "disk_usage_scan",
    "web_server_state",
}


class ScriptedModel:
    def __init__(self, turns: list[Turn]) -> None:
        self.turns = turns
        self.calls: list[set[str]] = []  # tool names offered on each call

    def create(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        self.calls.append({t["name"] for t in tools})
        turn = self.turns[len(self.calls) - 1]
        content = turn(messages) if callable(turn) else turn
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return ModelTurn(content, stop, {"input_tokens": 10, "output_tokens": 5}, "scripted")


def use(i: int, name: str, **args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}


def diagnosis(runbook: str) -> list[dict[str, Any]]:
    return [
        use(
            0,
            "submit_diagnosis",
            root_cause="cause",
            runbook_id=runbook,
            confidence="high",
            evidence=["observed"],
            ruled_out=[],
            suspicious_content=[],
        )
    ]


def submit_last_dry_run(i: int, action: str) -> Turn:
    """Submit the plan_hash that the previous dry_run_action returned (as a model would)."""

    def turn(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = messages[-1]["content"][0]["content"]
        found = re.search(r'"plan_hash": "([0-9a-f]+)"', result)
        assert found, result
        return [
            use(
                i,
                "submit_plan",
                action=action,
                plan_hash=found.group(1),
                rationale="fixes the cause",
                proposal="",
            )
        ]

    return turn


class Fleet:
    """One simulated host plus every service the graph needs, for one recorded case."""

    def __init__(self, case: str, recurs: bool = False) -> None:
        self.spec = json.loads(Path(f"evals/incidents/{case}.json").read_text(encoding="utf-8"))
        if recurs:
            self.spec["sim"] = {"recurs_after_fix": True}
        self.executor = SimExecutor([host_from_case(self.spec)])
        self.audit = InMemoryAuditLog()
        self.service = ActionService(self.executor, self.audit, kill_switch=lambda: False)
        self.diag: ToolBox = InProcessToolBox(
            build_server(FixtureBackend(Path(f"evals/incidents/{case}.json")))
        )
        self.saver = InMemorySaver()
        self.slept: list[float] = []
        self.thread = uuid.uuid4().hex

    def graph(self, model: ScriptedModel, runbook: str) -> Any:
        def retriever(query: str, k: int, platform: str | None = None) -> Sequence[RunbookHit]:
            return [RunbookHit(runbook, "t", "Symptoms", "text")]

        return build_incident_graph(
            model,
            self.diag,
            retriever,
            self.service,
            self.saver,
            verify_wait_seconds=90,
            sleep=self.slept.append,
        )

    def run(self, model: ScriptedModel, runbook: str, resume: Any = None) -> dict[str, Any]:
        cfg = run_config(self.thread)
        arg: Command[Any] | dict[str, Any] = (
            Command(resume=resume)
            if resume is not None
            else {"incident": self.spec["incident"], "run_id": self.thread}
        )
        return dict(self.graph(model, runbook).invoke(arg, cfg))

    def phases(self) -> list[str]:
        return [r.phase for r in self.audit.records]


@pytest.fixture
def cleanup() -> Iterator[list[Fleet]]:
    fleets: list[Fleet] = []
    yield fleets
    for f in fleets:
        f.diag.close()


def fleet(cleanup: list[Fleet], case: str, recurs: bool = False) -> Fleet:
    f = Fleet(case, recurs)
    cleanup.append(f)
    return f


# ---------------- Tier 0 / Tier 1: automatic, audited, verified ----------------


def test_tier0_disk_cleanup_end_to_end(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "disk_iis_logs")
    model = ScriptedModel(
        [
            [use(1, "disk_usage_scan", instance="fleet000002")],
            diagnosis("RB-001"),
            [use(2, "list_actions")],
            [use(3, "dry_run_action", action="delete_iis_logs_older_than", params={"days": 7})],
            submit_last_dry_run(4, "delete_iis_logs_older_than"),
        ]
    )
    state = f.run(model, "RB-001")
    assert state["outcome"] == "resolved", state.get("reason")
    assert state["verification"]["recovered"] and state["verification"]["after"] < 90
    assert f.phases() == ["before", "after", "verify"]
    assert f.slept == [90]  # verification waited before re-reading the metric


def test_agents_never_hold_tools_from_the_other_side(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "disk_iis_logs")
    model = ScriptedModel(
        [
            diagnosis("RB-001"),
            [use(1, "dry_run_action", action="clear_temp", params={"days": 7})],
            submit_last_dry_run(2, "clear_temp"),
        ]
    )
    f.run(model, "RB-001")
    diagnose_call, *planner_calls = model.calls
    assert diagnose_call >= DIAGNOSE_TOOLS and not diagnose_call & {"dry_run_action", "submit_plan"}
    for tools in planner_calls:
        assert tools == {"list_actions", "dry_run_action", "submit_plan"}
    assert not any("execute" in name for call in model.calls for name in call)


def test_linux_reload_releases_deleted_open_space(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "linux_deleted_open")
    model = ScriptedModel(
        [
            diagnosis("RB-010"),
            [
                use(
                    1,
                    "dry_run_action",
                    action="graceful_reload_service",
                    params={"service": "apache2"},
                )
            ],
            submit_last_dry_run(2, "graceful_reload_service"),
        ]
    )
    state = f.run(model, "RB-010")
    assert state["outcome"] == "resolved" and state["action_result"]["released_gb"] == 20.4


def test_fix_that_does_not_hold_escalates(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "pool_crash", recurs=True)
    model = ScriptedModel(
        [
            diagnosis("RB-003"),
            [
                use(
                    1,
                    "dry_run_action",
                    action="recycle_app_pool",
                    params={"pool": "DefaultAppPool"},
                )
            ],
            submit_last_dry_run(2, "recycle_app_pool"),
        ]
    )
    state = f.run(model, "RB-003")
    assert state["outcome"] == "escalated" and "not recovered" in state["reason"]
    assert f.phases() == ["before", "after", "verify"] and not f.audit.records[-1].ok


# ---------------- gates in the graph ----------------


def test_action_the_runbook_does_not_allow_is_blocked(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "pool_crash")
    model = ScriptedModel(
        [
            diagnosis("RB-006"),  # RB-006 allows restart_iis_services, not recycle_app_pool
            [
                use(
                    1,
                    "dry_run_action",
                    action="recycle_app_pool",
                    params={"pool": "DefaultAppPool"},
                )
            ],
            submit_last_dry_run(2, "recycle_app_pool"),
        ]
    )
    state = f.run(model, "RB-006")
    assert state["outcome"] == "escalated" and "citation" in state["reason"]
    assert f.executor.applied == [] and f.phases() == ["blocked"]


def test_plan_hash_must_come_from_a_dry_run(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "disk_iis_logs")
    model = ScriptedModel(
        [
            diagnosis("RB-001"),
            [
                use(
                    1,
                    "submit_plan",
                    action="clear_temp",
                    plan_hash="feedfacefeedface",
                    rationale="trust me",
                    proposal="",
                )
            ],
            [
                use(
                    2,
                    "submit_plan",
                    action="none",
                    plan_hash="",
                    rationale="",
                    proposal="Could not plan; please review.",
                )
            ],
        ]
    )
    state = f.run(model, "RB-001")
    rejected = state["plan_messages"][-3]["content"][0]  # tool result for the fake hash
    assert rejected["is_error"] and "dry_run_action" in rejected["content"]
    assert state["outcome"] == "proposed" and f.executor.applied == []


def test_propose_only_hands_humans_a_proposal(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "disk_freb")
    model = ScriptedModel(
        [
            diagnosis("RB-002"),
            [
                use(
                    1,
                    "submit_plan",
                    action="none",
                    plan_hash="",
                    rationale="",
                    proposal="Turn off Failed Request Tracing on fleet000001 (enabled 5h ago).",
                )
            ],
        ]
    )
    state = f.run(model, "RB-002")
    assert state["outcome"] == "proposed" and "Failed Request Tracing" in state["proposal"]
    assert f.executor.applied == [] and f.audit.records == []


# ---------------- Tier 2: impossible without a matching human approval ----------------


def tier2_model() -> ScriptedModel:
    return ScriptedModel(
        [
            diagnosis("RB-002"),
            [
                use(
                    1,
                    "dry_run_action",
                    action="delete_paths",
                    params={"paths": ["C:\\inetpub\\logs\\FailedReqLogFiles"]},
                )
            ],
            submit_last_dry_run(2, "delete_paths"),
        ]
    )


def test_tier2_pauses_and_runs_after_approval(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "disk_freb")
    model = tier2_model()
    paused = f.run(model, "RB-002")
    assert "__interrupt__" in paused and f.executor.applied == []
    request = paused["__interrupt__"][0].value
    assert request["type"] == "approval_required" and request["dry_run"]["target_count"] > 0
    plan_hash = request["plan"]["plan_hash"]

    state = f.run(
        model,
        "RB-002",
        {
            "approver_id": "j.ortiz",
            "plan_hash": plan_hash,
            "decision": "approve",
            "note": "FREB was left on",
        },
    )
    assert state["outcome"] == "resolved"
    assert f.phases() == ["approval", "before", "after", "verify"]
    assert f.audit.records[1].actor == "j.ortiz"


@pytest.mark.parametrize(
    "answer, outcome",
    [
        ({"approver_id": "j.ortiz", "plan_hash": "0" * 16, "decision": "approve"}, "escalated"),
        ({"approver_id": "agent", "plan_hash": "HASH", "decision": "approve"}, "escalated"),
        ({"approver_id": "", "plan_hash": "HASH", "decision": "approve"}, "escalated"),
        ({"approver_id": "j.ortiz", "plan_hash": "HASH", "decision": "reject"}, "rejected"),
    ],
    ids=["wrong-hash", "agent-approves-itself", "anonymous", "rejected"],
)
def test_tier2_bad_approvals_never_execute(
    cleanup: list[Fleet], answer: dict[str, str], outcome: str
) -> None:
    f = fleet(cleanup, "disk_freb")
    model = tier2_model()
    paused = f.run(model, "RB-002")
    real_hash = paused["__interrupt__"][0].value["plan"]["plan_hash"]
    answer = {k: (real_hash if v == "HASH" else v) for k, v in answer.items()}
    state = f.run(model, "RB-002", answer)
    assert state["outcome"] == outcome
    assert f.executor.applied == []
    assert "before" not in f.phases()


def test_host_changing_during_approval_blocks_the_action(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "disk_freb")
    model = tier2_model()
    paused = f.run(model, "RB-002")
    plan_hash = paused["__interrupt__"][0].value["plan"]["plan_hash"]
    # While the human was reading, a new trace file appeared: the approved list is now wrong.
    from fleet.governance.executor import SimFile

    f.executor.host("fleet000001").files.append(
        SimFile("C:\\inetpub\\logs\\FailedReqLogFiles\\fr999.xml", 0.2, 0)
    )
    state = f.run(
        model, "RB-002", {"approver_id": "j.ortiz", "plan_hash": plan_hash, "decision": "approve"}
    )
    assert state["outcome"] == "escalated" and "hash mismatch" in state["reason"]
    assert f.executor.applied == []


# ---------------- crash safety ----------------


def test_crash_after_action_cannot_run_it_twice(cleanup: list[Fleet]) -> None:
    f = fleet(cleanup, "disk_iis_logs")
    real_execute = f.service.execute
    crashed: list[bool] = []

    def execute_then_die(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = real_execute(*args, **kwargs)
        if not crashed:
            crashed.append(True)
            raise SystemExit("process killed after the action, before the checkpoint")
        return result

    f.service.execute = execute_then_die  # type: ignore[method-assign]
    model = ScriptedModel(
        [
            diagnosis("RB-001"),
            [use(1, "dry_run_action", action="delete_iis_logs_older_than", params={"days": 7})],
            submit_last_dry_run(2, "delete_iis_logs_older_than"),
        ]
    )
    with pytest.raises(SystemExit):
        f.run(model, "RB-001")
    state = dict(f.graph(model, "RB-001").invoke(None, run_config(f.thread)))
    assert len(f.executor.applied) == 1  # the replayed act step did not delete again
    assert state["outcome"] == "escalated" and "hash mismatch" in state["reason"]


# ---------------- approval across processes, via Postgres ----------------


def test_runner_pauses_and_resumes_tier2_through_postgres(monkeypatch: pytest.MonkeyPatch) -> None:
    import psycopg

    from fleet.agents.run import run_incident
    from fleet.detector.store import DEFAULT_DSN

    try:
        admin = psycopg.connect(DEFAULT_DSN, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("Postgres not running")
    schema = f"test_{uuid.uuid4().hex[:8]}"
    admin.execute(f'CREATE SCHEMA "{schema}"')
    monkeypatch.setenv("FLEET_PG_DSN", f"{DEFAULT_DSN}?options=-csearch_path%3D{schema}")
    try:

        def retriever(query: str, k: int, platform: str | None = None) -> Sequence[RunbookHit]:
            return [RunbookHit("RB-002", "t", "Symptoms", "text")]

        case, thread = Path("evals/incidents/disk_freb.json"), f"t-{uuid.uuid4().hex}"
        model = tier2_model()
        paused = run_incident(case, model, thread, retriever=retriever)
        plan_hash = paused["__interrupt__"][0].value["plan"]["plan_hash"]

        # A new run, as from a second CLI invocation: fresh graph, connection and host.
        answer = {"approver_id": "j.ortiz", "plan_hash": plan_hash, "decision": "approve"}
        state = run_incident(case, model, thread, resume=answer, retriever=retriever)
        assert state["outcome"] == "resolved"
        rows = admin.execute(
            f'SELECT phase, actor FROM "{schema}".audit_log ORDER BY id'
        ).fetchall()
        assert rows == [
            ("approval", "j.ortiz"),
            ("before", "j.ortiz"),
            ("after", "j.ortiz"),
            ("verify", "policy"),
        ]
    finally:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()
