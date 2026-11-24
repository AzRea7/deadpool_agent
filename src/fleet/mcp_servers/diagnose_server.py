"""Read-only diagnose MCP server. The diagnose agent's only window onto the fleet.

    FLEET_DIAGNOSE_CASE=evals/incidents/pool_crash.json \\
        uv run python -m fleet.mcp_servers.diagnose_server     # stdio transport

Separation of duties: this server only reads. Action tools live in a different server with
different credentials (phase 5), and the diagnose agent is never given them.
"""

import json
import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from fleet.mcp_servers.backend import BackendError, DiagnoseBackend, FixtureBackend

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def _result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True)


def build_server(backend: DiagnoseBackend) -> MCPServer:
    server: MCPServer = MCPServer(
        name="fleet-diagnose",
        instructions="Read-only diagnostics for Windows/IIS scale set instances.",
    )

    def safe(call: Any) -> str:
        try:
            return _result(call())
        except BackendError as e:
            return _result({"error": str(e)})

    @server.tool(annotations=READ_ONLY)
    def metric_series(
        instance: str, metric: Literal["disk_used_pct", "probe_ok"], hours: int = 6
    ) -> str:
        """Time series for one instance: C: used percent, or health probes (1 ok, 0 failed)."""
        return safe(lambda: backend.metric_series(instance, metric, hours))

    @server.tool(annotations=READ_ONLY)
    def event_log_tail(
        instance: str,
        log: Literal["System", "Application"],
        hours: int = 2,
        max_events: int = 20,
    ) -> str:
        """Recent Windows event log entries (newest first) for one instance."""
        return safe(lambda: backend.event_log_tail(instance, log, hours, max_events))

    @server.tool(annotations=READ_ONLY)
    def iis_log_summary(instance: str) -> str:
        """Request counts by HTTP status and HTTPERR reasons for the recent window."""
        return safe(lambda: backend.iis_log_summary(instance))

    @server.tool(annotations=READ_ONLY)
    def recent_deploys(hours: int = 72) -> str:
        """Deployments and configuration changes to the fleet in the last N hours."""
        return safe(lambda: backend.recent_deploys(hours))

    @server.tool(annotations=READ_ONLY)
    def disk_usage_scan(instance: str) -> str:
        """Largest folders on C: with size, file count and oldest file age. Reads only."""
        return safe(lambda: backend.disk_usage_scan(instance))

    @server.tool(annotations=READ_ONLY)
    def iis_state(instance: str) -> str:
        """App pool states and the WAS / W3SVC service status on one instance."""
        return safe(lambda: backend.iis_state(instance))

    return server


def backend_from_env() -> DiagnoseBackend:
    case = os.environ.get("FLEET_DIAGNOSE_CASE")
    if not case:
        raise SystemExit("Set FLEET_DIAGNOSE_CASE to a recorded case (Azure backend: phase 6).")
    return FixtureBackend(Path(case))


if __name__ == "__main__":
    build_server(backend_from_env()).run("stdio")
