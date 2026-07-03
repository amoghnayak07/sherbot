-- No vector extension yet — added in Phase 3

CREATE TABLE incidents (
    incident_id      UUID PRIMARY KEY,
    alert_name       TEXT NOT NULL,
    pod              TEXT NOT NULL,
    namespace        TEXT NOT NULL,
    scenario         TEXT,
    status           TEXT CHECK (status IN ('in_flight','completed','failed')),
    fixed            BOOLEAN DEFAULT FALSE,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE agent_runs (
    run_id           UUID PRIMARY KEY,
    incident_id      UUID REFERENCES incidents(incident_id),
    agent_name       TEXT NOT NULL,
    status           TEXT CHECK (status IN ('pending','running','completed','failed')),
    confidence       FLOAT CHECK (confidence BETWEEN 0.0 AND 1.0),
    data_found       BOOLEAN,
    completed_at     TIMESTAMPTZ
);

CREATE TABLE rca_reports (
    report_id            UUID PRIMARY KEY,
    incident_id          UUID REFERENCES incidents(incident_id),
    root_cause           TEXT,
    blast_radius         JSONB,
    contributing_factors JSONB,
    remediation_steps    JSONB,
    overall_confidence   FLOAT,
    status               TEXT DEFAULT 'completed',
    raw_output           TEXT,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE USER rca_agent WITH PASSWORD 'changeme';
GRANT SELECT, INSERT, UPDATE ON incidents, agent_runs, rca_reports TO rca_agent;
