"""Append-only audit log: a row before and after every action, and one for every block,
approval and verification. Postgres enforces append-only with a trigger; the in-memory log is
for tests.
"""

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

import psycopg
from psycopg.types.json import Jsonb

Phase = Literal["blocked", "approval", "before", "after", "verify"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL,
    incident_id text NOT NULL,
    run_id      text NOT NULL,
    instance    text NOT NULL,
    action      text NOT NULL,
    tier        integer NOT NULL,
    phase       text NOT NULL CHECK (phase IN ('blocked','approval','before','after','verify')),
    actor       text NOT NULL,
    runbook_id  text NOT NULL,
    plan_hash   text NOT NULL,
    ok          boolean NOT NULL,
    reason      text NOT NULL,
    params      jsonb NOT NULL,
    result      jsonb NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_instance_at ON audit_log (instance, at);

CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
"""


@dataclass(frozen=True)
class AuditRecord:
    at: datetime
    incident_id: str
    run_id: str
    instance: str
    action: str
    tier: int
    phase: Phase
    actor: str  # "policy" for Tier 0/1, the approver ID for Tier 2, "agent" for blocks
    runbook_id: str
    plan_hash: str
    ok: bool
    reason: str  # why: the plan rationale, the failed gate, or the verification verdict
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)


class AuditLog(Protocol):
    def append(self, record: AuditRecord) -> None: ...
    def actions_since(self, instance: str, since: datetime) -> list[AuditRecord]:
        """Successful 'after' rows for this instance since a time (rate limits, hysteresis)."""
        ...


class InMemoryAuditLog:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def append(self, record: AuditRecord) -> None:
        self.records.append(record)

    def actions_since(self, instance: str, since: datetime) -> list[AuditRecord]:
        return [
            r
            for r in self.records
            if r.instance == instance and r.phase == "after" and r.ok and r.at >= since
        ]


class PostgresAuditLog:
    def __init__(self, conn: psycopg.Connection[tuple[object, ...]]) -> None:
        self.conn = conn
        conn.execute(SCHEMA)

    def append(self, record: AuditRecord) -> None:
        row = asdict(record)
        row["params"], row["result"] = Jsonb(row["params"]), Jsonb(row["result"])
        cols = ", ".join(row)
        self.conn.execute(
            f"INSERT INTO audit_log ({cols}) VALUES ({', '.join(['%s'] * len(row))})",  # noqa: S608
            list(row.values()),
        )

    def actions_since(self, instance: str, since: datetime) -> list[AuditRecord]:
        names = list(AuditRecord.__dataclass_fields__)
        rows = self.conn.execute(
            f"SELECT {', '.join(names)} FROM audit_log "  # noqa: S608
            "WHERE instance = %s AND phase = 'after' AND ok AND at >= %s ORDER BY at",
            (instance, since),
        ).fetchall()
        return [AuditRecord(**dict(zip(names, r, strict=True))) for r in rows]  # type: ignore[arg-type]
