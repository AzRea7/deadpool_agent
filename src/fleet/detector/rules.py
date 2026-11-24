"""Detector rules (no LLM). Pure functions over samples: same input, same findings.

Starting values come from the scenarios table in the plan. The rules are OS-neutral; the host
inventory only changes labels (C: or /) and travels with the finding to diagnosis.
  disk_filling: system volume >= 90% used, or projected full within 12h from the 6h slope
  web_down:     3 failed health probes in 5 minutes (IIS app pool/service, or Apache)
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from fleet.detector.samples import DISK_USED_PCT, PROBE_OK, WINDOWS_IIS, HostInfo, Sample

Scenario = Literal["disk_filling", "web_down"]
Severity = Literal["warning", "critical"]

DISK_USED_WARN = 90.0
DISK_USED_CRITICAL = 95.0  # also the "act now even during hysteresis" line
PROJECTION_HOURS = 12.0
SLOPE_WINDOW = timedelta(hours=6)
MIN_SLOPE_POINTS = 6  # 30 minutes of 5-minute samples before trusting a slope
STALE_AFTER = timedelta(minutes=30)  # no fresh data: not our call (RB-008 covers blind spots)

PROBE_FAILS = 3
PROBE_WINDOW = timedelta(minutes=5)


@dataclass(frozen=True)
class Finding:
    instance: str
    scenario: Scenario
    severity: Severity
    reason: str
    evidence: dict[str, float | str] = field(default_factory=dict)
    host: HostInfo = WINDOWS_IIS


def slope_per_hour(points: Sequence[tuple[datetime, float]]) -> float:
    """Least-squares slope of value over time, in units per hour."""
    t0 = points[0][0]
    xs = [(t - t0).total_seconds() / 3600 for t, _ in points]
    ys = [v for _, v in points]
    n = len(points)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / var_x


def disk_rule(
    instance: str, samples: Iterable[Sample], now: datetime, host: HostInfo = WINDOWS_IIS
) -> Finding | None:
    points = sorted(
        (s.ts, s.value)
        for s in samples
        if s.metric == DISK_USED_PCT and now - SLOPE_WINDOW <= s.ts <= now
    )
    if not points or now - points[-1][0] > STALE_AFTER:
        return None
    used = points[-1][1]
    slope = slope_per_hour(points) if len(points) >= MIN_SLOPE_POINTS else 0.0
    hours_to_full = (100.0 - used) / slope if slope > 0 else None
    evidence: dict[str, float | str] = {
        "used_pct": used,
        "slope_pct_per_hour": round(slope, 2),
        "hours_to_full": round(hours_to_full, 1) if hours_to_full is not None else "n/a",
    }

    label = f"{host.volume} {used:.0f}% used"
    if used >= DISK_USED_CRITICAL:
        return Finding(instance, "disk_filling", "critical", label, evidence, host)
    if used >= DISK_USED_WARN:
        return Finding(instance, "disk_filling", "warning", label, evidence, host)
    if hours_to_full is not None and hours_to_full <= PROJECTION_HOURS:
        reason = f"{label}, projected full in {hours_to_full:.1f}h"
        return Finding(instance, "disk_filling", "warning", reason, evidence, host)
    return None


def probe_rule(
    instance: str, samples: Iterable[Sample], now: datetime, host: HostInfo = WINDOWS_IIS
) -> Finding | None:
    recent = [s for s in samples if s.metric == PROBE_OK and now - PROBE_WINDOW < s.ts <= now]
    failed = sum(s.value < 1 for s in recent)
    if failed < PROBE_FAILS:
        return None
    reason = f"{failed} of {len(recent)} health probes failed in {PROBE_WINDOW.seconds // 60} min"
    evidence: dict[str, float | str] = {"failed": failed, "probes": len(recent)}
    return Finding(instance, "web_down", "critical", reason, evidence, host)


def evaluate(
    samples: Iterable[Sample], now: datetime, hosts: dict[str, HostInfo] | None = None
) -> list[Finding]:
    """Run every rule for every instance seen in the samples."""
    hosts = hosts or {}
    by_instance: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        by_instance[s.instance].append(s)
    findings: list[Finding] = []
    for instance in sorted(by_instance):
        for rule in (disk_rule, probe_rule):
            finding = rule(instance, by_instance[instance], now, hosts.get(instance, WINDOWS_IIS))
            if finding is not None:
                findings.append(finding)
    return findings
