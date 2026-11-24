"""Diagnose agent with a scripted model: no API calls, runs in CI."""

import json
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver

from fleet.agents.diagnose import (
    MAX_MODEL_CALLS,
    ModelTurn,
    RunbookHit,
    build_graph,
    run_config,
)
from fleet.agents.toolbox import InProcessToolBox, ToolBox
from fleet.detector.store import DEFAULT_DSN
from fleet.mcp_servers.backend import FixtureBackend
from fleet.mcp_servers.diagnose_server import build_server

CASE = Path("evals/incidents/pool_crash.json")
INCIDENT = json.loads(CASE.read_text(encoding="utf-8"))["incident"]

RUNBOOKS = {
    "RB-003": RunbookHit("RB-003", "App pool crash or hang", "Symptoms", "503s, pool stopped"),
    "RB-006": RunbookHit("RB-006", "IIS service stopped", "Symptoms", "every site fails"),
    "RB-004": RunbookHit("RB-004", "Memory leak", "Symptoms", "w3wp memory climbs"),
    "RB-005": RunbookHit("RB-005", "5xx after deploy", "Symptoms", "5xx on every instance"),
}


SEARCHES: list[str | None] = []


def retriever(query: str, k: int, platform: str | None = None) -> Sequence[RunbookHit]:
    """Detector reason finds RB-003/006/004; a 'deploy' query finds RB-005."""
    SEARCHES.append(platform)
    ids = ["RB-005"] if "deploy" in query else ["RB-003", "RB-006", "RB-004"]
    return [RUNBOOKS[i] for i in ids][:k]


def tool_use(i: int, name: str, **args: Any) -> dict[str, Any]:
    return {"type": "tool_use", "id": f"tu_{i}", "name": name, "input": args}


def submit(i: int, runbook_id: str) -> dict[str, Any]:
    return tool_use(
        i,
        "submit_diagnosis",
        root_cause="w3wp crashes (StackOverflowException) until rapid-fail disables the pool",
        runbook_id=runbook_id,
        confidence="high",
        evidence=["WAS 5009 x2 then 5002", "DefaultAppPool Stopped", "HTTPERR AppOffline"],
        ruled_out=["RB-006: WAS and W3SVC are running"],
        suspicious_content=["Request URL asks to delete C:"],
    )


class ScriptedModel:
    """Returns pre-written turns in order and records every request."""

    def __init__(self, turns: list[list[dict[str, Any]]]) -> None:
        self.turns = turns
        self.requests: list[list[dict[str, Any]]] = []

    def create(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelTurn:
        assert any(t["name"] == "submit_diagnosis" for t in tools)
        assert not any("recycle" in t["name"] or "delete" in t["name"] for t in tools)
        self.requests.append(list(messages))
        content = self.turns[len(self.requests) - 1]
        stop = "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn"
        return ModelTurn(content, stop, {"input_tokens": 100, "output_tokens": 20}, "scripted")


@pytest.fixture
def toolbox() -> Iterator[ToolBox]:
    tb = InProcessToolBox(build_server(FixtureBackend(CASE)))
    yield tb
    tb.close()


def run(model: ScriptedModel, toolbox: ToolBox) -> dict[str, Any]:
    graph = build_graph(model, toolbox, retriever, InMemorySaver())
    return dict(graph.invoke({"incident": INCIDENT}, run_config(uuid.uuid4().hex)))


def test_happy_path_diagnoses_with_citation(toolbox: ToolBox) -> None:
    model = ScriptedModel(
        [
            [
                {"type": "text", "text": "Checking pool state."},
                tool_use(1, "web_server_state", instance="fleet000003"),
            ],
            [tool_use(2, "system_log_tail", instance="fleet000003", log="system")],
            [submit(3, "RB-003")],
        ]
    )
    state = run(model, toolbox)
    assert state["outcome"] == "diagnosed"
    assert state["diagnosis"]["runbook_id"] == "RB-003"
    assert state["model_calls"] == 3
    assert state["usage"] == {"input_tokens": 300, "output_tokens": 60}
    # The first message carries the incident and the prefetched candidates.
    first = model.requests[0][0]["content"]
    assert "fleet000003" in first and "[RB-003]" in first


def test_tool_output_is_wrapped_as_untrusted(toolbox: ToolBox) -> None:
    model = ScriptedModel(
        [
            [tool_use(1, "system_log_tail", instance="fleet000003", log="application")],
            [submit(2, "RB-003")],
        ]
    )
    run(model, toolbox)
    result = model.requests[1][-1]["content"][0]
    assert result["content"].startswith(
        '<tool_output tool="system_log_tail" trust="untrusted-data">'
    )
    assert "ignore previous instructions" in result["content"]  # delivered as data, quoted


def test_uncited_runbook_is_rejected_until_retrieved(toolbox: ToolBox) -> None:
    model = ScriptedModel(
        [
            [submit(1, "RB-005")],  # never retrieved: rejected
            [tool_use(2, "search_runbook", query="errors after deploy")],
            [submit(3, "RB-005")],  # now retrieved: accepted
        ]
    )
    state = run(model, toolbox)
    rejection = model.requests[1][-1]["content"][0]
    assert (
        rejection["is_error"] is True and "not returned by search_runbook" in rejection["content"]
    )
    assert state["outcome"] == "diagnosed" and state["diagnosis"]["runbook_id"] == "RB-005"
    assert "RB-005" in state["retrieved"]


def test_bad_tool_arguments_go_back_to_the_model(toolbox: ToolBox) -> None:
    model = ScriptedModel(
        [[tool_use(1, "web_server_state", instance="not an instance!")], [submit(2, "RB-003")]]
    )
    run(model, toolbox)
    result = model.requests[1][-1]["content"][0]
    assert result["is_error"] is True and "invalid instance" in result["content"]


def test_no_submit_gets_one_nudge_then_escalates(toolbox: ToolBox) -> None:
    text = [{"type": "text", "text": "Probably the app pool."}]
    model = ScriptedModel([text, text])
    state = run(model, toolbox)
    assert state["outcome"] == "escalated" and state["diagnosis"] is None
    assert model.requests[1][-1]["content"].startswith("Finish by calling submit_diagnosis")


def test_endless_investigation_is_capped(toolbox: ToolBox) -> None:
    turns = [
        [tool_use(i, "web_server_state", instance="fleet000003")]
        for i in range(MAX_MODEL_CALLS + 5)
    ]
    model = ScriptedModel(turns)
    state = run(model, toolbox)
    assert state["outcome"] == "escalated" and state["model_calls"] == MAX_MODEL_CALLS


class CrashOnce:
    """Wraps a toolbox and raises on its first call: a process dying mid-investigation."""

    def __init__(self, inner: ToolBox) -> None:
        self.inner, self.crashed = inner, False

    def specs(self) -> list[dict[str, Any]]:
        return self.inner.specs()

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("simulated crash")
        return self.inner.call(name, arguments)

    def close(self) -> None:
        self.inner.close()


def test_resumes_from_postgres_after_a_crash(toolbox: ToolBox) -> None:
    try:
        psycopg.connect(DEFAULT_DSN).close()
    except psycopg.OperationalError:
        pytest.skip("Postgres not running")
    turns = [
        [tool_use(1, "web_server_state", instance="fleet000003")],
        [tool_use(2, "system_log_tail", instance="fleet000003", log="system")],
        [submit(3, "RB-003")],
    ]
    model = ScriptedModel(turns)
    thread = f"test-{uuid.uuid4().hex}"
    flaky = CrashOnce(toolbox)

    with PostgresSaver.from_conn_string(DEFAULT_DSN) as saver:
        saver.setup()
        with pytest.raises(RuntimeError, match="simulated crash"):
            build_graph(model, flaky, retriever, saver).invoke(
                {"incident": INCIDENT}, run_config(thread)
            )
        assert len(model.requests) == 1  # the model call before the crash

    # A fresh graph and a fresh connection, as after a process restart.
    with PostgresSaver.from_conn_string(DEFAULT_DSN) as saver:
        state = build_graph(model, flaky, retriever, saver).invoke(None, run_config(thread))

    assert state["outcome"] == "diagnosed"
    assert len(model.requests) == 3  # resumed at the tools step: the first call wasn't repeated
    assert state["model_calls"] == 3


def test_runbook_search_is_filtered_to_the_host_platform(toolbox: ToolBox) -> None:
    SEARCHES.clear()
    model = ScriptedModel(
        [[tool_use(1, "search_runbook", query="pool keeps stopping")], [submit(2, "RB-003")]]
    )
    run(model, toolbox)
    assert SEARCHES == ["windows", "windows"]  # the prefetch and the model's own search
