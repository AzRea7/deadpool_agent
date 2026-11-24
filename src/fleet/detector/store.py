"""Postgres sample store and incident table for the detector."""

import os
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files

import psycopg
from psycopg.types.json import Jsonb

from fleet.detector.rules import Finding
from fleet.detector.samples import Sample

DEFAULT_DSN = "postgresql://postgres:dev@localhost:5432/fleet"  # local compose, dev only


def connect(dsn: str | None = None) -> psycopg.Connection[tuple[object, ...]]:
    return psycopg.connect(dsn or os.environ.get("FLEET_PG_DSN", DEFAULT_DSN), autocommit=True)


def init_schema(conn: psycopg.Connection[tuple[object, ...]]) -> None:
    conn.execute(files("fleet.detector").joinpath("schema.sql").read_text(encoding="utf-8"))


def save_samples(conn: psycopg.Connection[tuple[object, ...]], samples: list[Sample]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO samples (instance, metric, ts, value) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT DO NOTHING",
            [(s.instance, s.metric, s.ts, s.value) for s in samples],
        )


def load_samples(
    conn: psycopg.Connection[tuple[object, ...]], since: datetime, until: datetime
) -> list[Sample]:
    rows = conn.execute(
        "SELECT instance, metric, ts, value FROM samples WHERE ts > %s AND ts <= %s",
        (since, until),
    ).fetchall()
    return [Sample(str(i), str(m), t, float(v)) for i, m, t, v in rows]  # type: ignore[arg-type]


@dataclass(frozen=True)
class Upsert:
    incident_id: int
    opened: bool  # True: new incident. False: existing active incident touched.


def open_or_touch(
    conn: psycopg.Connection[tuple[object, ...]], finding: Finding, now: datetime
) -> Upsert:
    """Open an incident for this finding, or bump the active one for the same instance+scenario.

    The partial unique index makes this race-safe: two detector runs cannot both open one.
    Severity only ever goes up while the incident is active.
    """
    row = conn.execute(
        """
        INSERT INTO incidents
            (instance, scenario, severity, reason, evidence, opened_at, last_seen_at,
             os, web_server)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (instance, scenario) WHERE closed_at IS NULL DO UPDATE SET
            occurrences  = incidents.occurrences + 1,
            last_seen_at = EXCLUDED.last_seen_at,
            reason       = EXCLUDED.reason,
            evidence     = EXCLUDED.evidence,
            severity     = CASE WHEN EXCLUDED.severity = 'critical'
                                THEN 'critical' ELSE incidents.severity END
        RETURNING id, (xmax = 0)
        """,
        (
            finding.instance,
            finding.scenario,
            finding.severity,
            finding.reason,
            Jsonb(finding.evidence),
            now,
            now,
            finding.host.os,
            finding.host.web_server,
        ),
    ).fetchone()
    assert row is not None
    return Upsert(incident_id=int(row[0]), opened=bool(row[1]))  # type: ignore[call-overload]


def resolve(conn: psycopg.Connection[tuple[object, ...]], incident_id: int, now: datetime) -> None:
    conn.execute(
        "UPDATE incidents SET status = 'resolved', closed_at = %s WHERE id = %s",
        (now, incident_id),
    )
