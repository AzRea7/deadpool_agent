"""Read-only diagnose MCP server. The diagnose agent's only window onto the fleet.

    FLEET_DIAGNOSE_CASE=evals/incidents/pool_crash.json \\
        uv run python -m fleet.mcp_servers.diagnose_server     # stdio transport

Works for Windows/IIS and Linux/Apache hosts: tool names are OS-neutral and each result names
the host type and the underlying source. Separation of duties: this server only reads. Action
tools live in a different server with different credentials (phase 5), and the diagnose agent
is never given them.
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
        instructions="Read-only diagnostics for Windows/IIS and Linux/Apache scale set instances.",
    )

    def safe(call: Any) -> str:
        try:
            return _result(call())
        except BackendError as e:
            return _result({"error": str(e)})

    @server.tool(annotations=READ_ONLY)
    def metric_series(
        instance: str,
        metric: Literal["disk_used_pct", "probe_ok", "available_memory_mb"],
        hours: int = 6,
    ) -> str:
        """Time series for one instance: system volume used % (C: or /), health probes
        (1 ok, 0 failed) or available memory in MB."""
        return safe(lambda: backend.metric_series(instance, metric, hours))

    @server.tool(annotations=READ_ONLY)
    def system_log_tail(
        instance: str,
        log: Literal["system", "application"],
        hours: int = 2,
        max_events: int = 20,
    ) -> str:
        """Recent log entries, newest first. Windows: System or Application event log.
        Linux: system = systemd journal (kernel, units), application = Apache error log."""
        return safe(lambda: backend.system_log_tail(instance, log, hours, max_events))

    @server.tool(annotations=READ_ONLY)
    def web_log_summary(instance: str) -> str:
        """Recent web server traffic: requests by HTTP status, plus IIS HTTPERR reasons or
        Apache error codes and the slowest or most failing URLs."""
        return safe(lambda: backend.web_log_summary(instance))

    @server.tool(annotations=READ_ONLY)
    def recent_deploys(hours: int = 72) -> str:
        """Deployments and configuration changes to the fleet in the last N hours."""
        return safe(lambda: backend.recent_deploys(hours))

    @server.tool(annotations=READ_ONLY)
    def disk_usage_scan(instance: str) -> str:
        """Largest folders on the system volume with size and oldest file age. On Linux also
        df vs du totals and deleted-but-open files (lsof +L1). Reads only."""
        return safe(lambda: backend.disk_usage_scan(instance))

    @server.tool(annotations=READ_ONLY)
    def web_server_state(instance: str) -> str:
        """Web server health. Windows: app pool states and WAS/W3SVC services. Linux: the
        apache2 systemd unit, apachectl configtest and worker usage."""
        return safe(lambda: backend.web_server_state(instance))

    return server


def backend_from_env() -> DiagnoseBackend:
    case = os.environ.get("FLEET_DIAGNOSE_CASE")
    if not case:
        raise SystemExit("Set FLEET_DIAGNOSE_CASE to a recorded case (Azure backend: phase 6).")
    return FixtureBackend(Path(case))


if __name__ == "__main__":
    build_server(backend_from_env()).run("stdio")
