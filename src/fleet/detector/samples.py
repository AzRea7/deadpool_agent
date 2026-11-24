"""Metric samples and where they come from.

The detector only sees the MetricsSource interface. FakeSource feeds it synthetic signals for
local runs and tests; an Azure Monitor / Log Analytics source plugs in later without changing
the rules or the job.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

DISK_USED_PCT = "disk_used_pct"  # C: used, 0-100
PROBE_OK = "probe_ok"  # health probe result: 1 passed, 0 failed

_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Sample:
    instance: str
    metric: str
    ts: datetime
    value: float


class MetricsSource(Protocol):
    def fetch(self, since: datetime, until: datetime) -> list[Sample]:
        """Samples with since < ts <= until."""
        ...


Signal = Callable[[datetime], float]


def ticks(since: datetime, until: datetime, step: timedelta) -> Iterator[datetime]:
    """Grid points (multiples of step from a fixed epoch) with since < t <= until.

    A fixed grid means consecutive fetch windows never produce the same timestamp twice.
    """
    t = _EPOCH + ((since - _EPOCH) // step + 1) * step
    while t <= until:
        yield t
        t += step


@dataclass
class FakeSource:
    """Synthetic metrics: signals[(instance, metric)] is a function of time."""

    signals: dict[tuple[str, str], Signal]
    steps: dict[str, timedelta] = field(
        default_factory=lambda: {
            DISK_USED_PCT: timedelta(minutes=5),
            PROBE_OK: timedelta(minutes=1),
        }
    )

    def fetch(self, since: datetime, until: datetime) -> list[Sample]:
        return [
            Sample(instance, metric, t, round(signal(t), 2))
            for (instance, metric), signal in sorted(self.signals.items())
            for t in ticks(since, until, self.steps[metric])
        ]


# ---- signal shapes, used by the simulator and tests ----


def steady(value: float) -> Signal:
    return lambda t: value


def ramp(base: float, start: datetime, per_hour: float, cap: float = 99.0) -> Signal:
    """Flat at base until start, then grows per_hour (a disk filling), capped."""

    def signal(t: datetime) -> float:
        hours = max(0.0, (t - start).total_seconds() / 3600)
        return min(cap, base + hours * per_hour)

    return signal


def fails_from(start: datetime) -> Signal:
    """Health probe passes until start, then fails (an app pool stopped)."""
    return lambda t: 0.0 if t >= start else 1.0
