"""Run the diagnose agent on a recorded incident case, with real Claude and the real MCP server.

    uv run python -m fleet.agents.run --case evals/incidents/pool_crash.json
    uv run python -m fleet.agents.run --case ... --thread demo1 --kill-after-tools 2   # dies
    uv run python -m fleet.agents.run --case ... --thread demo1 --resume               # resumes

Needs ANTHROPIC_API_KEY (or another SDK credential), Postgres and the Qdrant runbook index.
"""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver

from fleet.agents.diagnose import AnthropicModel, Model, build_graph, rag_retriever, run_config
from fleet.agents.toolbox import StdioToolBox, ToolBox
from fleet.detector.store import DEFAULT_DSN

# Claude Opus 5.5, $ per million tokens.
PRICE = {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00}


def cost_usd(usage: dict[str, int]) -> float:
    return (
        usage.get("input_tokens", 0) * PRICE["input"]
        + usage.get("output_tokens", 0) * PRICE["output"]
        + usage.get("cache_read_input_tokens", 0) * PRICE["cache_read"]
        + usage.get("cache_creation_input_tokens", 0) * PRICE["cache_write"]
    ) / 1_000_000


class KillAfter:
    """Hard-kills the process after N tool calls, to demo resume after a real crash."""

    def __init__(self, inner: ToolBox, n: int) -> None:
        self.inner, self.n, self.calls = inner, n, 0

    def specs(self) -> list[dict[str, Any]]:
        return self.inner.specs()

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        if self.calls >= self.n:
            print(f"\n*** killing the process before tool call {self.calls + 1} ({name}) ***")
            sys.stdout.flush()
            os._exit(137)
        self.calls += 1
        return self.inner.call(name, arguments)

    def close(self) -> None:
        self.inner.close()


def run_case(
    case: Path,
    model: Model,
    thread_id: str,
    resume: bool = False,
    kill_after_tools: int | None = None,
) -> dict[str, Any]:
    incident = json.loads(case.read_text(encoding="utf-8"))["incident"]
    env = dict(os.environ, FLEET_DIAGNOSE_CASE=str(case))
    toolbox: ToolBox = StdioToolBox("fleet.mcp_servers.diagnose_server", env=env)
    if kill_after_tools is not None:
        toolbox = KillAfter(toolbox, kill_after_tools)
    dsn = os.environ.get("FLEET_PG_DSN", DEFAULT_DSN)
    try:
        with PostgresSaver.from_conn_string(dsn) as saver:
            saver.setup()
            graph = build_graph(model, toolbox, rag_retriever(), saver)
            state = graph.invoke(None if resume else {"incident": incident}, run_config(thread_id))
            return dict(state)
    finally:
        toolbox.close()


def tool_calls(state: dict[str, Any]) -> list[str]:
    return [
        f"{b['name']}({json.dumps(b.get('input', {}), sort_keys=True)})"
        for m in state.get("messages", [])
        if m["role"] == "assistant"
        for b in m["content"]
        if b.get("type") == "tool_use"
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", type=Path, required=True)
    parser.add_argument("--thread", default=None, help="checkpoint thread id (default: new)")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--kill-after-tools", type=int, default=None)
    parser.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh"])
    args = parser.parse_args()

    thread = args.thread or f"{args.case.stem}-{uuid.uuid4().hex[:8]}"
    print(f"thread: {thread}")
    state = run_case(
        args.case, AnthropicModel(effort=args.effort), thread, args.resume, args.kill_after_tools
    )
    print("\ntool calls:")
    for call in tool_calls(state):
        print("  ", call)
    print(f"\noutcome: {state['outcome']} {state.get('reason', '')}")
    print(json.dumps(state.get("diagnosis"), indent=2))
    usage = state["usage"]
    print(f"\nmodel calls: {state['model_calls']}  tokens: {usage}  cost: ${cost_usd(usage):.3f}")


if __name__ == "__main__":
    main()
