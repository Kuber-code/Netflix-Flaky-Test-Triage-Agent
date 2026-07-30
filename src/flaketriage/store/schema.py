"""SQLite schema and forward-only migrations.

**Shape.** ``executions`` is an append-only fact table with narrow, mostly
scalar columns; ``runs``, ``test_identities`` and the diff tables are small
dimensions. That is deliberate. The one query that matters at scale -- "all
outcomes for this identity over the last N executions" -- is a single indexed
scan of one column-oriented-friendly table.

**Migration path off SQLite.** If execution volume outgrows a single file, the
move is to load ``executions`` into a columnar store partitioned by ingest date,
keeping the dimensions in whatever relational store is at hand. Nothing in the
schema depends on SQLite semantics: no triggers, no views, no autoincrement
gaps, no JSON columns on the hot path. Text timestamps are stored as ISO-8601
UTC, which every engine can parse.

**Why ``''`` and not ``NULL`` for optional keys.** SQLite treats NULLs as
distinct in UNIQUE constraints, so ``UNIQUE(run_id, attempt, shard_id)`` would
not deduplicate an unsharded re-ingest. Absent shard and worker ids normalize to
the empty string so idempotency actually holds.
"""

from __future__ import annotations

from typing import Final

SCHEMA_VERSION: Final = 2

_MIGRATION_1: Final = """
CREATE TABLE runs (
    id            INTEGER PRIMARY KEY,
    run_id        TEXT    NOT NULL,
    attempt       INTEGER NOT NULL DEFAULT 1,
    commit_sha    TEXT    NOT NULL,
    branch        TEXT    NOT NULL DEFAULT '',
    shard_id      TEXT    NOT NULL DEFAULT '',
    worker_id     TEXT    NOT NULL DEFAULT '',
    started_at    TEXT    NOT NULL,
    ingested_at   TEXT    NOT NULL,
    UNIQUE (run_id, attempt, shard_id)
);

CREATE INDEX idx_runs_sha ON runs (commit_sha);
CREATE INDEX idx_runs_started ON runs (started_at);

CREATE TABLE test_identities (
    id          INTEGER PRIMARY KEY,
    fingerprint TEXT    NOT NULL UNIQUE,
    suite_path  TEXT    NOT NULL,
    test_name   TEXT    NOT NULL,
    parameters  TEXT    NOT NULL DEFAULT '',
    file_path   TEXT,
    first_seen  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL
);

CREATE INDEX idx_identities_logical ON test_identities (suite_path, test_name);

-- Rename tolerance (phase P2). `certain = 0` means the merge was inferred from
-- a similarity score rather than observed directly, and must be surfaced in
-- output as merged_uncertain rather than applied silently.
CREATE TABLE identity_aliases (
    id              INTEGER PRIMARY KEY,
    old_identity_id INTEGER NOT NULL REFERENCES test_identities (id),
    new_identity_id INTEGER NOT NULL REFERENCES test_identities (id),
    similarity      REAL    NOT NULL,
    certain         INTEGER NOT NULL DEFAULT 0,
    observed_run_id INTEGER REFERENCES runs (id),
    created_at      TEXT    NOT NULL,
    UNIQUE (old_identity_id, new_identity_id)
);

-- Append-only fact table. One row per (test, run).
CREATE TABLE executions (
    id              INTEGER PRIMARY KEY,
    run_pk          INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    identity_id     INTEGER NOT NULL REFERENCES test_identities (id),
    outcome         TEXT    NOT NULL
                    CHECK (outcome IN ('pass', 'fail', 'error', 'skip')),
    duration_ms     INTEGER,
    failure_type    TEXT,
    failure_message TEXT,
    stack_trace     TEXT,
    stdout          TEXT,
    stderr          TEXT,
    rerun_observed  INTEGER NOT NULL DEFAULT 0,
    source_file     TEXT,
    created_at      TEXT    NOT NULL,
    UNIQUE (run_pk, identity_id)
);

CREATE INDEX idx_executions_identity ON executions (identity_id, id DESC);
CREATE INDEX idx_executions_run ON executions (run_pk);

CREATE TABLE diff_files (
    id          INTEGER PRIMARY KEY,
    run_pk      INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    path        TEXT    NOT NULL,
    old_path    TEXT,
    change_type TEXT    NOT NULL,
    binary      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_pk, path)
);

CREATE TABLE diff_hunks (
    id           INTEGER PRIMARY KEY,
    diff_file_id INTEGER NOT NULL REFERENCES diff_files (id) ON DELETE CASCADE,
    side         TEXT    NOT NULL CHECK (side IN ('new', 'old')),
    start_line   INTEGER NOT NULL,
    end_line     INTEGER NOT NULL
);

CREATE INDEX idx_diff_hunks_file ON diff_hunks (diff_file_id);

-- Parse problems are persisted, not just logged. A run whose results were
-- half-truncated must be visibly degraded in the report rather than quietly
-- reported as clean.
CREATE TABLE parse_warnings (
    id         INTEGER PRIMARY KEY,
    run_pk     INTEGER REFERENCES runs (id) ON DELETE CASCADE,
    origin     TEXT    NOT NULL,
    reason     TEXT    NOT NULL,
    detail     TEXT,
    created_at TEXT    NOT NULL
);

CREATE INDEX idx_parse_warnings_run ON parse_warnings (run_pk);
"""


# Phase P6: observability. Every model call and every classification is persisted,
# not merely logged. A log line answers "what happened just now"; these tables
# answer "what does this tool cost us per week, and how often does it abstain" --
# which is the question that decides whether it stays switched on.
_MIGRATION_2: Final = """
CREATE TABLE llm_calls (
    id             INTEGER PRIMARY KEY,
    run_pk         INTEGER REFERENCES runs (id) ON DELETE SET NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('prefilter', 'classify', 'repair')),
    model          TEXT    NOT NULL,
    prompt_version TEXT    NOT NULL DEFAULT '',
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL    NOT NULL DEFAULT 0,
    latency_ms     REAL    NOT NULL DEFAULT 0,
    cache_hit      INTEGER NOT NULL DEFAULT 0,
    schema_valid   INTEGER NOT NULL DEFAULT 1,
    error          TEXT,
    created_at     TEXT    NOT NULL
);

CREATE INDEX idx_llm_calls_created ON llm_calls (created_at);
CREATE INDEX idx_llm_calls_kind ON llm_calls (kind, created_at);

-- One row per classification produced, including abstentions. Abstentions are
-- stored rather than skipped: the abstention rate is a headline metric, and it
-- cannot be computed from a table that only records successes.
CREATE TABLE classifications (
    id               INTEGER PRIMARY KEY,
    identity_id      INTEGER NOT NULL REFERENCES test_identities (id),
    run_pk           INTEGER REFERENCES runs (id) ON DELETE SET NULL,
    commit_sha       TEXT    NOT NULL DEFAULT '',
    cause            TEXT    NOT NULL,
    confidence       REAL    NOT NULL DEFAULT 0,
    abstained        INTEGER NOT NULL DEFAULT 1,
    downgrade_reason TEXT    NOT NULL DEFAULT 'none',
    reasoning        TEXT,
    evidence         TEXT,
    suggested_action TEXT,
    model            TEXT    NOT NULL DEFAULT '',
    prompt_version   TEXT    NOT NULL DEFAULT '',
    cache_hit        INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL
);

CREATE INDEX idx_classifications_identity ON classifications (identity_id, id DESC);
CREATE INDEX idx_classifications_created ON classifications (created_at);
"""

# Forward-only. Index i applies version i+1; never edit a shipped entry.
MIGRATIONS: Final[tuple[str, ...]] = (_MIGRATION_1, _MIGRATION_2)
