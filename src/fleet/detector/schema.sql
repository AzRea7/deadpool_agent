-- Detector sample store and incidents. Idempotent: safe to apply on every start.

CREATE TABLE IF NOT EXISTS samples (
    instance text NOT NULL,
    metric   text NOT NULL,
    ts       timestamptz NOT NULL,
    value    double precision NOT NULL,
    PRIMARY KEY (instance, metric, ts)
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples (ts);

-- One row per problem. Lifecycle (later phases move it along):
--   open -> diagnosing -> awaiting_approval -> acting -> verifying -> resolved
--   any active status -> escalated (a human owns it now)
CREATE TABLE IF NOT EXISTS incidents (
    id           bigserial PRIMARY KEY,
    instance     text NOT NULL,
    scenario     text NOT NULL,
    status       text NOT NULL DEFAULT 'open' CHECK (status IN (
                     'open', 'diagnosing', 'awaiting_approval', 'acting',
                     'verifying', 'resolved', 'escalated')),
    severity     text NOT NULL CHECK (severity IN ('warning', 'critical')),
    reason       text NOT NULL,
    evidence     jsonb NOT NULL,
    opened_at    timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    closed_at    timestamptz,
    occurrences  integer NOT NULL DEFAULT 1,
    CHECK ((status = 'resolved') = (closed_at IS NOT NULL))
);

-- What runs on the instance, so diagnosis searches the right runbooks and tools.
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS os text NOT NULL DEFAULT 'windows';
ALTER TABLE incidents ADD COLUMN IF NOT EXISTS web_server text NOT NULL DEFAULT 'iis';

-- The de-duplication rule: at most one active incident per instance and scenario.
-- An escalated incident stays active, so the detector never re-opens what a human is handling.
CREATE UNIQUE INDEX IF NOT EXISTS incidents_one_active
    ON incidents (instance, scenario) WHERE closed_at IS NULL;
