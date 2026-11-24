"""Read-only data behind the diagnose MCP server.

Every tool is a fixed, parameterized query: the model picks parameters from enums and bounded
ranges, never writes KQL or PowerShell. FixtureBackend replays a recorded incident case
(evals/incidents/*.json); an Azure backend (Log Analytics + Run Command reads) plugs in later
behind the same interface.
"""

import json
import re
from pathlib import Path
from typing import Any, Literal, Protocol

Metric = Literal["disk_used_pct", "probe_ok"]
EventLog = Literal["System", "Application"]

INSTANCE_RE = re.compile(r"^[a-z0-9-]{3,40}$")

# The KQL each tool stands for, shown in results so a human can rerun it in Log Analytics.
KQL = {
    "metric_series": (
        "Perf | where Computer == '{instance}' and CounterName == '{counter}' "
        "| where TimeGenerated > ago({hours}h) | project TimeGenerated, CounterValue"
    ),
    "event_log_tail": (
        "Event | where Computer == '{instance}' and EventLog == '{log}' "
        "| where TimeGenerated > ago({hours}h) | top {max_events} by TimeGenerated desc"
    ),
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
    def event_log_tail(
        self, instance: str, log: EventLog, hours: int, max_events: int
    ) -> dict[str, Any]: ...
    def iis_log_summary(self, instance: str) -> dict[str, Any]: ...
    def recent_deploys(self, hours: int) -> dict[str, Any]: ...
    def disk_usage_scan(self, instance: str) -> dict[str, Any]: ...
    def iis_state(self, instance: str) -> dict[str, Any]: ...


class FixtureBackend:
    """Replays one recorded incident case. Times in the file are minutes/hours ago."""

    def __init__(self, case_path: Path) -> None:
        self.case: dict[str, Any] = json.loads(case_path.read_text(encoding="utf-8"))

    def _known(self, instance: str) -> str:
        check_instance(instance)
        if instance != self.case["incident"]["instance"]:
            raise BackendError(f"no data for instance {instance!r}")
        return instance

    def metric_series(self, instance: str, metric: Metric, hours: int) -> dict[str, Any]:
        self._known(instance)
        check_range("hours", hours, 1, 24)
        if metric not in ("disk_used_pct", "probe_ok"):
            raise BackendError(f"unknown metric {metric!r}")
        points = self.case["metrics"].get(f"{instance}|{metric}", [])
        series = [{"minutes_ago": m, "value": v} for m, v in points if m <= hours * 60]
        query = KQL["metric_series"].format(instance=instance, counter=metric, hours=hours)
        return {"metric": metric, "points": series, "kql": query}

    def event_log_tail(
        self, instance: str, log: EventLog, hours: int, max_events: int
    ) -> dict[str, Any]:
        self._known(instance)
        check_range("hours", hours, 1, 72)
        check_range("max_events", max_events, 1, 50)
        if log not in ("System", "Application"):
            raise BackendError(f"unknown log {log!r}")
        events = [
            e
            for e in self.case["events"].get(f"{instance}|{log}", [])
            if e["minutes_ago"] <= hours * 60
        ]
        events = sorted(events, key=lambda e: e["minutes_ago"])[:max_events]
        query = KQL["event_log_tail"].format(
            instance=instance, log=log, hours=hours, max_events=max_events
        )
        return {"log": log, "events": events, "kql": query}

    def iis_log_summary(self, instance: str) -> dict[str, Any]:
        self._known(instance)
        return dict(self.case["iis"][instance])

    def recent_deploys(self, hours: int) -> dict[str, Any]:
        check_range("hours", hours, 1, 168)
        return {"deploys": [d for d in self.case["deploys"] if d["hours_ago"] <= hours]}

    def disk_usage_scan(self, instance: str) -> dict[str, Any]:
        self._known(instance)
        return {"drive": "C:", "folders": self.case["disk_scan"].get(instance, [])}

    def iis_state(self, instance: str) -> dict[str, Any]:
        self._known(instance)
        return {
            "app_pools": self.case["app_pools"].get(instance, []),
            "services": self.case["services"].get(instance, []),
        }
