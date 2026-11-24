"""Action MCP server: the only process that can change an instance (ADR-004).

Separation of duties: this server runs with the action credentials (in Azure, a managed
identity allowed to use Run Command on the scale set); the diagnose server and the diagnose
agent never see them. Every execute call goes through ActionService, which re-runs all gates,
requires a matching human approval for Tier 2, and writes the audit log, so a caller cannot
skip a check by calling this server directly.

    FLEET_ACTION_CASE=evals/incidents/disk_iis_logs.json \\
        uv run python -m fleet.mcp_servers.action_server     # stdio, simulated host
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from fleet.governance.allowlist import AllowlistError
from fleet.governance.audit import InMemoryAuditLog
from fleet.governance.catalog import CatalogError
from fleet.governance.executor import SimExecutor, host_from_case
from fleet.governance.service import ActionService, Approval, GateError, IncidentContext, Plan

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
CHANGES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False)


def _ctx(context: dict[str, Any]) -> IncidentContext:
    return IncidentContext(**{**context, "retrieved": tuple(context.get("retrieved", ()))})


def _plan(plan: dict[str, Any]) -> Plan:
    return Plan(**{**plan, "dry_run_at": datetime.fromisoformat(plan["dry_run_at"])})


def build_server(service: ActionService) -> MCPServer:
    server: MCPServer = MCPServer(
        name="fleet-actions",
        instructions="Catalog actions for scale set instances. Every call is gated and audited.",
    )

    def safe(call: Any) -> str:
        try:
            return json.dumps(call(), sort_keys=True, default=str)
        except GateError as e:
            r = e.report
            return json.dumps({"error": str(e), "failed_gate": r.failed_gate, "tier": r.tier})
        except (CatalogError, AllowlistError, KeyError, TypeError, ValueError) as e:
            return json.dumps({"error": str(e)})

    @server.tool(annotations=READ_ONLY)
    def list_actions(platform: str, runbook_id: str) -> str:
        """Catalog actions for a platform that the given runbook allows, with tiers and schemas."""
        return safe(lambda: {"actions": service.list_actions(platform, runbook_id)})

    @server.tool(annotations=READ_ONLY)
    def dry_run_action(context: dict[str, Any], action: str, params: dict[str, Any]) -> str:
        """List exactly what an action would change on the incident's instance, with a hash."""
        return safe(lambda: service.dry_run(_ctx(context), action, params).summary())

    @server.tool(annotations=CHANGES)
    def execute_action(
        context: dict[str, Any], plan: dict[str, Any], approval: dict[str, Any] | None = None
    ) -> str:
        """Run a planned action after every gate passes. Tier 2 needs a matching approval."""

        def run() -> dict[str, Any]:
            ctx, p = _ctx(context), _plan(plan)
            ok = Approval(**approval) if approval else None
            before = service.read_metric(ctx)
            result = service.execute(p, ctx, ok)
            verdict = service.verify(p, ctx, before)
            return {"result": result, "verification": verdict.__dict__}

        return safe(run)

    return server


def service_from_env() -> ActionService:
    case = os.environ.get("FLEET_ACTION_CASE")
    if not case:
        raise SystemExit("Set FLEET_ACTION_CASE to a recorded case (Azure executor: phase 6).")
    host = host_from_case(json.loads(Path(case).read_text(encoding="utf-8")))
    return ActionService(SimExecutor([host]), InMemoryAuditLog())


if __name__ == "__main__":
    build_server(service_from_env()).run("stdio")
