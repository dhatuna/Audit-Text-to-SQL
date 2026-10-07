-- Append-only audit log. One row per question, holding every step of the pipeline.

CREATE SCHEMA audit;

CREATE TABLE audit.query_log (
    request_id          TEXT PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL,
    user_label          TEXT NOT NULL,
    question            TEXT NOT NULL,
    context_version     TEXT NOT NULL,
    context_sha256      TEXT NOT NULL,
    context_text        TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    outcome             TEXT NOT NULL
        CHECK (outcome IN ('answered', 'clarified', 'refused', 'blocked', 'failed')),
    attempts            JSONB NOT NULL,
    final_sql           TEXT,
    result_fingerprint  TEXT,
    answer              JSONB,
    grounding           JSONB,
    total_latency_ms    DOUBLE PRECISION NOT NULL,
    error               TEXT
);
CREATE INDEX query_log_created_at ON audit.query_log (created_at DESC);
CREATE INDEX query_log_outcome ON audit.query_log (outcome);

-- Grants alone stop the app role from rewriting history; the trigger also stops the
-- table owner and anyone granted UPDATE later by mistake.
CREATE FUNCTION audit.reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.query_log is append-only (% rejected)', TG_OP;
END;
$$;

CREATE TRIGGER query_log_append_only
    BEFORE UPDATE OR DELETE ON audit.query_log
    FOR EACH ROW EXECUTE FUNCTION audit.reject_mutation();

CREATE TRIGGER query_log_no_truncate
    BEFORE TRUNCATE ON audit.query_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_mutation();
