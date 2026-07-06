CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE embeddings (
    embedding_id     UUID PRIMARY KEY,
    incident_id      UUID REFERENCES incidents(incident_id),
    source           TEXT CHECK (source IN ('logs','traces')),
    content          TEXT,
    metadata         JSONB,
    embedding        vector(384),
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX ON embeddings USING hnsw (embedding vector_cosine_ops);

GRANT SELECT, INSERT, UPDATE ON embeddings TO rca_agent;
