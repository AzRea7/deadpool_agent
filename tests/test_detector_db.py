"""Detector + Postgres: incidents open once and de-duplicate. Needs Postgres (compose or CI)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from fleet.detector.job import simulate
from fleet.detector.rules import Finding
from fleet.detector.store import connect, init_schema, open_or_touch, resolve

START = datetime(2026, 12, 1, 8, 0, tzinfo=UTC)

Conn = psycopg.Connection[tuple[object, ...]]


@pytest.fixture
def conn() -> Iterator[Conn]:
    """A throwaway schema per test, so tests never see each other's rows or real data."""
    try:
        c = connect()
    except psycopg.OperationalError:
        pytest.skip("Postgres not running (docker compose -f infra/docker-compose.yml up -d)")
    schema = f"test_{uuid.uuid4().hex[:8]}"
    c.execute(f'CREATE SCHEMA "{schema}"')
    c.execute(f'SET search_path TO "{schema}"')
    init_schema(c)
    try:
        yield c
    finally:
        c.execute(f'DROP SCHEMA "{schema}" CASCADE')
        c.close()


def incidents(c: Conn) -> list[tuple[object, ...]]:
    return c.execute(
        "SELECT instance, scenario, severity, occurrences, status FROM incidents ORDER BY id"
    ).fetchall()


def test_healthy_fleet_opens_nothing(conn: Conn) -> None:
    results = simulate(conn, "healthy", START, hours=4)
    assert all(not r.findings for r in results)
    assert incidents(conn) == []


def test_disk_fill_opens_one_incident_not_many(conn: Conn) -> None:
    # Fill starts at +1h: 58% growing 8%/h, so 90% at +5h and 98% at +6h.
    results = simulate(conn, "fill", START, hours=6)
    rows = incidents(conn)
    assert len(rows) == 1
    instance, scenario, severity, occurrences, status = rows[0]
    assert (instance, scenario, status) == ("sim-web0", "disk_filling", "open")
    assert isinstance(occurrences, int) and occurrences > 1  # kept firing, de-duplicated
    assert severity == "critical"  # escalated in place, not a second incident
    assert sum(len(r.opened) for r in results) == 1
    # Nothing fires during the healthy first hour.
    assert all(not r.findings for r in results[:4])
    # Caught early by the growth projection, long before the 90% line.
    first = next(r for r in results if r.opened)
    used = first.findings[0].evidence["used_pct"]
    assert isinstance(used, float) and used < 75


def test_pool_down_opens_one_incident(conn: Conn) -> None:
    simulate(conn, "pool", START, hours=3)
    rows = incidents(conn)
    assert [(r[0], r[1]) for r in rows] == [("sim-web1", "app_pool_down")]


def test_resolved_incident_allows_a_new_one(conn: Conn) -> None:
    finding = Finding("web0", "disk_filling", "warning", "C: 91% used")
    first = open_or_touch(conn, finding, START)
    again = open_or_touch(conn, finding, START + timedelta(minutes=15))
    assert first.opened and not again.opened
    assert first.incident_id == again.incident_id

    resolve(conn, first.incident_id, START + timedelta(hours=1))
    later = open_or_touch(conn, finding, START + timedelta(hours=2))
    assert later.opened and later.incident_id != first.incident_id


def test_severity_never_drops_while_active(conn: Conn) -> None:
    open_or_touch(conn, Finding("web0", "disk_filling", "critical", "C: 96% used"), START)
    open_or_touch(conn, Finding("web0", "disk_filling", "warning", "C: 91% used"), START)
    assert incidents(conn)[0][2] == "critical"


def test_escalated_incident_blocks_reopening(conn: Conn) -> None:
    finding = Finding("web0", "app_pool_down", "critical", "3 of 5 probes failed")
    first = open_or_touch(conn, finding, START)
    conn.execute("UPDATE incidents SET status = 'escalated' WHERE id = %s", (first.incident_id,))
    again = open_or_touch(conn, finding, START + timedelta(minutes=15))
    assert not again.opened and again.incident_id == first.incident_id
