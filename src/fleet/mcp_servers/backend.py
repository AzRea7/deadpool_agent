"""Read-only data behind the diagnose MCP server, for Windows/IIS and Linux/Apache hosts.

Every tool is a fixed, parameterized query: the model picks parameters from enums and bounded
ranges, never writes KQL, PowerShell or shell. Tool names are OS-neutral; each result says
which host type it came from and what the underlying source was (event log or journald,
IIS or Apache logs). FixtureBackend replays a recorded incident case (evals/incidents/*.json);
an Azure backend (Log Analytics + Run Command reads) plugs in later behind the same interface.
"""

import json
import re
from pathlib import Path
from typing import Any, Literal, Protocol

Metric = Literal["disk_used_pct", "probe_ok", "available_memory_mb"]
LogKind = Literal["system", "application"]

INSTANCE_RE = re.compile(r"^[a-z0-9-]{3,40}$")

# What each log kind means per OS (shown in results so the model knows what it is reading).
LOG_SOURCES = {
    ("windows", "system"): "Windows System event log",
    ("windows", "application"): "Windows Application event log",
    ("linux", "system"): "systemd journal (kernel and units)",
    ("linux", "application"): "Apache error log",
}

# The KQL each tool stands for, so a human can rerun it in Log Analytics.
KQL = {
    ("windows", "metric_series"): (
        "Perf | where Computer == '{instance}' and CounterName == '{counter}' "
        "| where TimeGenerated > ago({hours}h) | project TimeGenerated, CounterValue"
    ),
    ("linux", "metric_series"): (
        "Perf | where Computer == '{instance}' and CounterName == '{counter}' "
        "and InstanceName in ('/', '_Total') "
        "| where TimeGenerated > ago({hours}h) | project TimeGenerated, CounterValue"
    ),
    ("windows", "system_log_tail"): (
        "Event | where Computer == '{instance}' and EventLog == '{log}' "
        "| where TimeGenerated > ago({hours}h) | top {max_events} by TimeGenerated desc"
    ),
    ("linux", "system_log_tail"): (
        "Syslog | where Computer == '{instance}' and Facility in ({facilities}) "
        "| where TimeGenerated > ago({hours}h) | top {max_events} by TimeGenerated desc"
    ),
}
COUNTERS = {
    ("windows", "disk_used_pct"): "% Free Space (as 100 - value)",
    ("linux", "disk_used_pct"): "% Used Space",
    ("windows", "available_memory_mb"): "Available MBytes",
    ("linux", "available_memory_mb"): "Available MBytes Memory",
    ("windows", "probe_ok"): "health probe",
    ("linux", "probe_ok"): "health probe",
}


class BackendError(ValueError):
    """Bad parameters. Returned to the model as a tool error, never raised to the user."""


def check_instance(instance: str) -> str:
    if not INSTANCE_RE.fullmatch(instance):
        raise BackendError(f"invalid instance name {instance!r}")
    return instance


def check_range(name: str, value: int, low: int, high: int) -> int:
    if not low <= value <= high:
        raise BackendError(f"{name} must be between {low} and {high}")
    return value


class DiagnoseBackend(Protocol):
    def metric_series(self, instance: str, metric: Metric, hours: int) -> dict[str, Any]: ...
    def system_log_tail(
        self, instance: str, log: LogKind, hours: int, max_events: int
    ) -> dict[str, Any]: ...
    def web_log_summary(self, instance: str) -> dict[str, Any]: ...
    def recent_deploys(self, hours: int) -> dict[str, Any]: ...
    def disk_usage_scan(self, instance: str) -> dict[str, Any]: ...
    def web_server_state(self, instance: str) -> dict[str, Any]: ...


class FixtureBackend:
    """Replays one recorded incident case. Times in the file are minutes/hours ago."""

    def __init__(self, case_path: Path) -> None:
        self.case: dict[str, Any] = json.loads(case_path.read_text(encoding="utf-8"))
        incident = self.case["incident"]
        self.instance: str = incident["instance"]
        self.os: str = incident.get("os", "windows")
        self.web_server: str = incident.get("web_server", "iis")

    def _known(self, instance: str) -> str:
        check_instance(instance)
        if instance != self.instance:
            raise BackendError(f"no data for instance {instance!r}")
        return instance

    def _host(self) -> dict[str, str]:
        return {"os": self.os, "web_server": self.web_server}

    def metric_series(self, instance: str, metric: Metric, hours: int) -> dict[str, Any]:
        self._known(instance)
        check_range("hours", hours, 1, 24)
        if metric not in ("disk_used_pct", "probe_ok", "available_memory_mb"):
            raise BackendError(f"unknown metric {metric!r}")
        points = self.case["metrics"].get(f"{instance}|{metric}", [])
        series = [{"minutes_ago": m, "value": v} for m, v in points if m <= hours * 60]
        counter = COUNTERS[(self.os, metric)]
        query = KQL[(self.os, "metric_series")].format(
            instance=instance, counter=counter, hours=hours
        )
        return {"host": self._host(), "metric": metric, "points": series, "kql": query}

    def system_log_tail(
        self, instance: str, log: LogKind, hours: int, max_events: int
    ) -> dict[str, Any]:
        self._known(instance)
        check_range("hours", hours, 1, 72)
        check_range("max_events", max_events, 1, 50)
        if log not in ("system", "application"):
            raise BackendError(f"unknown log {log!r}")
        events = [
            e
            for e in self.case["events"].get(f"{instance}|{log}", [])
            if e["minutes_ago"] <= hours * 60
        ]
        events = sorted(events, key=lambda e: e["minutes_ago"])[:max_events]
        if self.os == "windows":
            query = KQL[("windows", "system_log_tail")].format(
                instance=instance, log=log.capitalize(), hours=hours, max_events=max_events
            )
        else:
            facilities = "'kern', 'daemon', 'syslog'" if log == "system" else "'local0'"
            query = KQL[("linux", "system_log_tail")].format(
                instance=instance, facilities=facilities, hours=hours, max_events=max_events
            )
        return {
            "host": self._host(),
            "log": log,
            "source": LOG_SOURCES[(self.os, log)],
            "events": events,
            "kql": query,
        }

    def web_log_summary(self, instance: str) -> dict[str, Any]:
        self._known(instance)
        return {"host": self._host(), **self.case["web_log"][instance]}

    def recent_deploys(self, hours: int) -> dict[str, Any]:
        check_range("hours", hours, 1, 168)
        return {"deploys": [d for d in self.case["deploys"] if d["hours_ago"] <= hours]}

    def disk_usage_scan(self, instance: str) -> dict[str, Any]:
        self._known(instance)
        scan = self.case["disk_scan"].get(instance, {})
        volume = "C:" if self.os == "windows" else "/"
        return {"host": self._host(), "volume": volume, **scan}

    def web_server_state(self, instance: str) -> dict[str, Any]:
        self._known(instance)
        return {"host": self._host(), **self.case["web_server"].get(instance, {})}
