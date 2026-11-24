"""Dry runs and plan hashes.

A dry run lists exactly what an action would change: files and bytes, or services and their
current state. Its hash covers instance, action, parameters and targets. A real run must present
a fresh hash that matches a dry run computed at execution time, so if anything changed on the
host since the plan was made (or since a human approved it), the action does not run.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def plan_hash(
    instance: str, action: str, params: dict[str, Any], targets: list[dict[str, Any]]
) -> str:
    body = json.dumps(
        {"instance": instance, "action": action, "params": params, "targets": targets},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class DryRun:
    instance: str
    action: str
    params: dict[str, Any]
    targets: list[dict[str, Any]]
    at: datetime
    hash: str = field(default="")

    @staticmethod
    def of(
        instance: str,
        action: str,
        params: dict[str, Any],
        targets: list[dict[str, Any]],
        at: datetime,
    ) -> "DryRun":
        return DryRun(
            instance, action, params, targets, at, plan_hash(instance, action, params, targets)
        )

    @property
    def total_gb(self) -> float:
        return round(sum(float(t.get("gb", 0.0)) for t in self.targets), 3)

    def summary(self) -> dict[str, Any]:
        return {
            "instance": self.instance,
            "action": self.action,
            "params": self.params,
            "targets": self.targets[:50],
            "target_count": len(self.targets),
            "total_gb": self.total_gb,
            "plan_hash": self.hash,
            "at": self.at.isoformat(timespec="seconds"),
        }
