-- Shared by the Rust sensor and the Python brain. Both open the same file.
-- Timestamps are UTC ISO-8601 strings with milliseconds, so they sort as text.

CREATE TABLE IF NOT EXISTS targets (
    name              TEXT PRIMARY KEY,
    kind              TEXT NOT NULL CHECK (kind IN ('http', 'tcp', 'heartbeat')),
    endpoint          TEXT NOT NULL,
    expected_status   INTEGER,
    latency_budget_ms INTEGER NOT NULL CHECK (latency_budget_ms > 0),
    depends_on        TEXT NOT NULL DEFAULT '[]',
    stateless         INTEGER NOT NULL DEFAULT 0 CHECK (stateless IN (0, 1)),
    restart           TEXT,
    rollback          TEXT,
    max_age_secs      INTEGER,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS probes (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    target     TEXT NOT NULL,
    path       TEXT NOT NULL CHECK (path IN ('primary', 'secondary', 'canary', 'brain')),
    ok         INTEGER NOT NULL CHECK (ok IN (0, 1)),
    status     INTEGER,
    latency_ms REAL,
    error      TEXT
);
CREATE INDEX IF NOT EXISTS probes_target_ts ON probes (target, ts);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    target     TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('failure', 'drift', 'recovered')),
    fault      TEXT NOT NULL CHECK (fault IN ('target', 'dependency', 'probe', 'none')),
    culprit    TEXT,
    score      REAL,
    detail     TEXT NOT NULL DEFAULT '{}',
    claimed_by INTEGER
);
CREATE INDEX IF NOT EXISTS events_unclaimed ON events (claimed_by, ts);

CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    event_id    INTEGER UNIQUE REFERENCES events (id),
    target      TEXT NOT NULL,
    opened_at   TEXT NOT NULL,
    closed_at   TEXT,
    state       TEXT NOT NULL,
    verdict     TEXT,
    tier        TEXT CHECK (tier IS NULL OR tier IN ('low', 'medium', 'high')),
    action      TEXT,
    outcome     TEXT,
    cost        TEXT NOT NULL DEFAULT '{}',
    report_path TEXT,
    report_md   TEXT
);
CREATE INDEX IF NOT EXISTS incidents_open ON incidents (closed_at);

CREATE TABLE IF NOT EXISTS transitions (
    id          INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents (id),
    ts          TEXT NOT NULL,
    state       TEXT NOT NULL,
    input       TEXT NOT NULL,
    output      TEXT NOT NULL,
    duration_ms REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS transitions_incident ON transitions (incident_id, id);

CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY,
    incident_id  INTEGER NOT NULL REFERENCES incidents (id),
    action       TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_at   TEXT,
    decision     TEXT NOT NULL DEFAULT 'pending' CHECK (decision IN ('pending', 'approved', 'denied', 'expired'))
);

CREATE TABLE IF NOT EXISTS supervisor (
    id       INTEGER PRIMARY KEY,
    ts       TEXT NOT NULL,
    task     TEXT NOT NULL,
    reason   TEXT NOT NULL,
    restarts INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS heartbeat (
    component TEXT PRIMARY KEY,
    ts        TEXT NOT NULL,
    note      TEXT
);
