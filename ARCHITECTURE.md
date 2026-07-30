# Architecture

This document covers structure and data flow. The *reasoning* behind the
structure lives in the [ADRs](docs/adr/); where a choice was contentious, this
file points at the one that argues it.

## The one rule that shapes everything

```
ingest ─┐
        ├─> store ──> identity ──> detect ──> policy ──> report
        │                             │          ▲
        │                             │          │ evidence only
        │                             └──> classify (the only package that
        │                                   talks to a model)
        └─> obs (logging + metrics, used by all)
```

**Nothing on the deterministic path imports `classify`.** Not `detect`, not
`policy`, not `report`, not `store`. The classifier is a leaf that other layers
hand data to and read results from, never a dependency they need in order to
work.

This is not a convention. `tests/test_detect_pipeline.py` walks the AST of every
module in the deterministic packages and fails if an import of
`flaketriage.classify` appears anywhere in them. Without that test, `--no-llm`
would decay from a guarantee into a hope — and it decayed once already: the
markdown renderer legitimately needed the `Classification` type, which is how the
shared vocabulary ended up in `models.py` rather than in the classifier package.
The types are shared; only the machinery that makes network calls is confined.
See [ADR-0001](docs/adr/0001-deterministic-core-llm-advisory.md).

**A second direction rule, learned the hard way: `store` knows nothing about
`policy`.** Quarantine persistence was briefly implemented as methods on
`RunStore`, which created a cycle — `policy` needs `detect`, `detect` needs
`store`, and `store` then needed `policy` for its type annotations. It resolved
fine in the order the CLI imports things and blew up the moment anything imported
`flaketriage.policy` first. The SQL now lives in `policy/records.py` behind a
`QuarantineStore` facade that takes a connection it does not own.

Two tests guard this: one imports every module in a fresh subprocess, because a
test process has already imported everything and cannot detect a cycle by
importing again; the other asserts the specific `store -> policy` edge is absent,
because a cycle test tells you *that* something is wrong rather than which edge to
delete.

## Data flow

```
JUnit XML          git diff            run metadata
(pytest, Surefire, (--unified=0 or     (sha, branch, run id,
 jest, Playwright,  a patch file)       attempt, shard, worker)
 nested reporters)
   │                   │                    │
   └───────────────────┴────────────────────┘
                       ▼
        ┌──────────────────────────────┐
        │ ingest                       │  streaming parse (iterparse),
        │  junit.py  diff.py           │  malformed input degrades to a
        │  pipeline.py                 │  persisted warning, never a crash
        └──────────────┬───────────────┘
                       ▼
        ┌──────────────────────────────┐
        │ identity                     │  normalize (suite_path, test_name,
        │  fingerprint.py similarity.py│  parameters) -> hash; reconcile
        │  alias.py  reconcile.py      │  renames and moves via an alias table
        └──────────────┬───────────────┘
                       ▼
        ┌──────────────────────────────┐
        │ store (SQLite)               │  runs, test_identities,
        │  schema.py db.py             │  identity_aliases, executions,
        │  repositories.py             │  diff_files/hunks, parse_warnings,
        │                              │  llm_calls, classifications,
        │                              │  quarantines
        └──────────────┬───────────────┘
                       ▼
        ┌──────────────────────────────┐
        │ detect            NO LLM     │  per-commit windows -> 4 signals,
        │  history.py rates.py         │  separate regression path,
        │  infra.py footprint.py       │  EWMA flake rate, confidence levels
        │  detector.py                 │
        └──────────────┬───────────────┘
                       │
          ┌────────────┴───────────────┐
          ▼                            ▼
┌───────────────────────┐   ┌──────────────────────────┐
│ classify              │   │ policy                   │
│  cache.py (content-   │   │  quarantine.py           │
│   addressed, keyed on │   │  ownership.py records.py │
│   context+model+prompt│   │                          │
│  prompt.py schema.py  │──>│  4 conditions, TTL,      │
│  client.py            │   │  owner, exit condition   │
│  classifier.py        │   │  (model input can only   │
│  pricing.py wiring.py │   │   veto, never cause)     │
└───────────┬───────────┘   └────────────┬─────────────┘
            └────────────┬───────────────┘
                         ▼
              ┌──────────────────────┐
              │ report               │
              │  renderers.py        │  terminal / JSON / markdown
              │  window.py           │
              └──────────┬───────────┘
                ┌────────┴────────┐
                ▼                 ▼
            CLI output      PR comment (upserted)
```

## Why each layer is shaped the way it is

### ingest

**Streaming, and tolerant.** `iterparse` with per-element release, because result
files from a large suite run to tens of megabytes. Tolerant because real CI
produces truncated XML when a worker is killed: such a file yields the cases
parsed before the truncation point plus a persisted warning. Losing a whole run's
results because a closing tag is missing is strictly worse than partial data.

**Five dialects, five fixtures.** "JUnit XML" is a family of dialects, not a
schema. `failure` and `error` are kept distinct because an assertion failure
points at the code under test while a harness error points at the environment, and
that distinction feeds classification. Surefire's `<flakyFailure>` is recorded as
a rerun observation — same-commit divergence asserted by the runner itself.

**A `DOCTYPE` is refused outright.** `iterparse` in 3.12 cannot be handed a
hardened parser, and a DOCTYPE has no legitimate purpose in a CI artifact, so the
document head is screened before parsing.

### identity

The key is a hashed, normalized `(suite_path, test_name, parameters)` triple.
Renames and moves are reconciled through an alias table with a combined
name-and-path distance, and merges above a strict threshold are stored as
`merged_uncertain` and surfaced rather than applied silently. A test renamed *and*
moved in one commit loses its history on purpose — both signals changed, so there
is no evidence distinguishing it from an unrelated deletion plus addition.
[ADR-0002](docs/adr/0002-test-identity-strategy.md).

### store

`executions` is an append-only fact table with narrow scalar columns; `runs`,
`test_identities` and the diff tables are small dimensions. The query that matters
at scale — all outcomes for one identity over the last N executions — is a single
indexed scan.

**Migration path off SQLite:** load `executions` into a columnar store partitioned
by ingest date and keep the dimensions relational. Nothing depends on SQLite
semantics: no triggers, no views, no JSON on the hot path, ISO-8601 UTC text
timestamps. Migrations are forward-only and have now run three times (P1, P6, P7),
which is the point of having built them that way.

**Writes are idempotent.** CI retries ingest steps, so `UNIQUE (run_pk,
identity_id)` plus `DO NOTHING` makes a re-ingest a no-op. Optional keys normalize
to `''` rather than `NULL`, because SQLite treats NULLs as distinct in UNIQUE
constraints and an unsharded re-ingest would otherwise double every observation
count.

### detect

**Per-commit windows, not per-row.** A test with four shards and two attempts
produces eight rows for one commit; reading those as eight independent
observations would make any sharded suite look wildly unstable.

**Precedence, not scoring.** The four signals are consulted in a fixed order
because they are not commensurable. Same-commit divergence wins outright.
Regression is checked *before* the historical-instability signal that a pass-to-fail
transition would otherwise trip, because emitting a real defect as noise is the
most expensive thing this tool can do. A first-ever failure is `NEW_FAILURE`, not
a guess.

**Infrastructure is excluded deterministically**, by configured patterns, before
any model runs — a flake rate that were only correct with the LLM enabled would not
be a deterministic core.

**The flake rate divides by all commits**, not by retried ones. Real pipelines
retry only on failure, so a denominator of retried commits reads near 100% for a
test failing one run in ten. It is exponentially weighted so a fixed test recovers.

### classify

Everything network-facing is behind a `ModelClient` protocol narrow enough that
the whole guardrail chain is testable against a scripted fake — which matters
because the behaviours claimed here (malformed output never raises, budget
exhaustion degrades) are exactly what a live API will not reproduce on request.

Order: cache, budget check, prefilter, classify, validate, repair once, abstain.
Every failure mode ends in `UNKNOWN` with a distinct machine-readable reason.
[ADR-0003](docs/adr/0003-abstention-over-guessing.md),
[ADR-0005](docs/adr/0005-two-tier-model-cost-strategy.md).

### policy

Four conditions, all required. The model contributes one field and that field can
only veto. Every recommendation carries a TTL, an owner with its provenance
labelled, and an exit condition. A quarantined test keeps running, because the exit
condition is consecutive clean executions and a test that stops running can never
satisfy it. [ADR-0004](docs/adr/0004-quarantine-ttl.md).

### report

Three renderers over one `Detection` list, so no two can disagree about a verdict.
The markdown form is ASCII-only and carries a hidden marker so the Action can
update its own comment instead of posting another.

## Storage schema

| table | grain | added |
|---|---|---|
| `runs` | one CI execution of the suite | P1 |
| `test_identities` | one logical test instance | P1 |
| `identity_aliases` | one observed rename or move | P1 (used P2) |
| `executions` | one (test, run) — the fact table | P1 |
| `diff_files` / `diff_hunks` | changed files and line ranges per run | P1 |
| `parse_warnings` | one recoverable ingest problem | P1 |
| `llm_calls` | one model call, with cost and latency | P6 |
| `classifications` | one proposed cause, including abstentions | P6 |
| `quarantines` | one quarantine lifecycle | P7 |

Two constraints worth calling out because they encode a decision rather than a
shape:

- `UNIQUE (run_pk, identity_id)` on `executions` is what makes re-ingest safe.
- A **partial** unique index on `quarantines (identity_id) WHERE state IN
  ('recommended','active')` enforces at most one open quarantine per test in the
  database rather than in the code that happens to write it — because "recommend it
  twice" is exactly what a retried CI job would otherwise do.

## Failure modes and what happens

| input | behaviour |
|---|---|
| Truncated XML | Cases before the truncation point, plus a persisted warning |
| Empty / non-XML / unreadable file | Warning, run still recorded |
| `DOCTYPE` present | Refused before parsing |
| Malformed model JSON | One repair attempt, then `UNKNOWN` with the sample logged |
| Cause outside the taxonomy | `UNKNOWN` with reason `unknown_cause` |
| Cause with no evidence | `UNKNOWN` with reason `no_evidence` |
| Confidence below the floor | `UNKNOWN` with reason `below_confidence_floor` |
| API error, rate limit, timeout | `UNKNOWN` with reason `api_error`; run continues |
| Budget exhausted | Remaining tests `UNKNOWN` with reason `budget_exhausted` |
| No `ANTHROPIC_API_KEY` | Deterministic path runs; reported, not an error |
| Corrupt cache entry | Treated as a miss and deleted |
| Store schema newer than the build | Refused loudly rather than misread |

## Testing strategy

- **Property-based** (`hypothesis`) on identity: any single-character rename
  preserves history, and — the one that matters — any pair beyond the distance
  ceiling is never merged. A merge rule without a false-merge guard is worse than
  no merge rule.
- **Fixture-driven** on ingest: one real XML file per dialect, plus truncated,
  empty, non-XML and DOCTYPE cases.
- **Scripted-client** on classify: twenty parametrized garbage payloads, forced
  schema failures, injected API errors, exhausted budgets.
- **Structural**: the import-graph test; a test that the emitted JSON schema avoids
  the keywords the API rejects; a test that `action.yml` only passes flags the CLI
  accepts.
- **An autouse fixture deletes `ANTHROPIC_API_KEY`** for the whole suite, so the
  tests cannot spend money or depend on a network.

Coverage on the deterministic core is 85–100% per module (`detect/`, `identity/`,
`policy/`), against the 80% floor in the specification.

## Deliberately not built

Chat interface, automatic PR creation that edits test code, web dashboard,
multi-model ensembles, fine-tuning, embedding retrieval, and any autonomous action
that changes CI state without a human. Scope discipline is a design choice; §13 of
[the specification](flaky-triage-agent-REQUIREMENTS.md) lists these as
anti-requirements and they stayed that way.
