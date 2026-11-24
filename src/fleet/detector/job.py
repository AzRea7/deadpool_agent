"""The detector job (no LLM): fetch samples, store them, apply rules, open or touch incidents.

Runs every 15 minutes in production (Container Apps Job, phase 6). Locally:

    uv run python -m fleet.detector.job --sim fill         # C: fills on sim-web0 (Windows/IIS)
    uv run python -m fleet.detector.job --sim fill-linux   # / fills on sim-lx0 (Linux/Apache)
    uv run python -m fleet.detector.job --sim down         # IIS stops answering on sim-web0
    uv run python -m fleet.detector.job --sim down-linux   # Apache stops answering on sim-lx0
    uv run python -m fleet.detector.job --sim healthy

The simulator replays the scenario on a simulated clock, so 4 hours run in a few seconds.
"""

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import psycopg
import structlog

from fleet.detector.rules import SLOPE_WINDOW, Finding, evaluate
from fleet.detector.samples import (
    DISK_USED_PCT,
    LINUX_APACHE,
    PROBE_OK,
    WINDOWS_IIS,
    FakeSource,
    MetricsSource,
    Signal,
    fails_from,
    ramp,
    steady,
)
from fleet.detector.store import connect, init_schema, load_samples, open_or_touch, save_samples

CYCLE = timedelta(minutes=15)
log = structlog.get_logger()


@dataclass
class CycleResult:
    samples: int = 0
    findings: list[Finding] = field(default_factory=list)
    opened: list[int] = field(default_factory=list)
    touched: list[int] = field(default_factory=list)


def run_once(
    source: MetricsSource, conn: psycopg.Connection[tuple[object, ...]], now: datetime
) -> CycleResult:
    fresh = source.fetch(now - CYCLE, now)
    save_samples(conn, fresh)
    # Rules see the stored history, not just this cycle, so the 6h slope works.
    findings = evaluate(load_samples(conn, now - SLOPE_WINDOW, now), now, source.hosts())
    result = CycleResult(samples=len(fresh), findings=findings)
    for finding in findings:
        upsert = open_or_touch(conn, finding, now)
        (result.opened if upsert.opened else result.touched).append(upsert.incident_id)
    log.info(
        "detector cycle",
        at=now.isoformat(timespec="minutes"),
        samples=result.samples,
        findings=len(findings),
        opened=result.opened,
        touched=result.touched,
        model_calls=0,  # the detector never calls a model; a healthy fleet costs nothing
    )
    return result


SIM_HOSTS = {"sim-web0": WINDOWS_IIS, "sim-lx0": LINUX_APACHE}
SIM_INSTANCES = tuple(SIM_HOSTS)
SCENARIOS = {  # name -> (instance it breaks, what breaks)
    "healthy": ("", ""),
    "fill": ("sim-web0", "disk"),
    "fill-linux": ("sim-lx0", "disk"),
    "down": ("sim-web0", "probe"),
    "down-linux": ("sim-lx0", "probe"),
}


def sim_source(scenario: str, start: datetime) -> FakeSource:
    """A Windows/IIS and a Linux/Apache instance; the scenario breaks one at start + 1h."""
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario {scenario!r}")
    trouble = start + timedelta(hours=1)
    signals: dict[tuple[str, str], Signal] = {
        ("sim-web0", DISK_USED_PCT): steady(58.0),
        ("sim-lx0", DISK_USED_PCT): steady(58.0),
        ("sim-web0", PROBE_OK): steady(1.0),
        ("sim-lx0", PROBE_OK): steady(1.0),
    }
    instance, what = SCENARIOS[scenario]
    if what == "disk":
        signals[(instance, DISK_USED_PCT)] = ramp(58.0, trouble, per_hour=8.0)
    elif what == "probe":
        signals[(instance, PROBE_OK)] = fails_from(trouble)
    return FakeSource(signals, inventory=dict(SIM_HOSTS))


def simulate(
    conn: psycopg.Connection[tuple[object, ...]], scenario: str, start: datetime, hours: float
) -> list[CycleResult]:
    conn.execute("DELETE FROM samples WHERE instance = ANY(%s)", (list(SIM_INSTANCES),))
    conn.execute("DELETE FROM incidents WHERE instance = ANY(%s)", (list(SIM_INSTANCES),))
    source = sim_source(scenario, start)
    cycles = int(timedelta(hours=hours) / CYCLE)
    return [run_once(source, conn, start + CYCLE * (i + 1)) for i in range(cycles)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", choices=list(SCENARIOS), required=True)
    parser.add_argument("--hours", type=float, default=4.0)
    args = parser.parse_args()

    start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=args.hours)
    with connect() as conn:
        init_schema(conn)
        results = simulate(conn, args.sim, start, args.hours)
        rows = conn.execute(
            "SELECT id, instance, os, web_server, scenario, severity, occurrences, reason "
            "FROM incidents "
            "WHERE instance = ANY(%s) ORDER BY id",
            (list(SIM_INSTANCES),),
        ).fetchall()
    opened = sum(len(r.opened) for r in results)
    print(f"\n{len(results)} cycles, {opened} incident(s) opened, 0 model calls")
    for row in rows:
        print("  incident", *row)


if __name__ == "__main__":
    main()
