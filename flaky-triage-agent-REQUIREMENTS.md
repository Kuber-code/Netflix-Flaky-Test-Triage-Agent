# Flaky Test Triage Agent — Requirements & Build Specification

> **How to use this document:** This is the complete build specification. Read it fully before writing code. Implement phase by phase in the order given. Do not skip Phase 5 (evaluation) — it is the point of the project, not an afterthought.

---

## 0. Context and intent

This is a portfolio project demonstrating **LLM-powered developer tooling built with engineering discipline** — not a prompt wrapper. It is aimed at a platform/developer-productivity engineering audience. The reader is expected to be skeptical of LLM projects, so the repository must prove:

1. A **deterministic core** that works without any LLM at all
2. An **LLM layer with measured accuracy**, not asserted accuracy
3. **Explicit cost, latency and failure-mode handling**
4. **Documented trade-offs** including what was deliberately not built

The single most important artifact in this repository is not the agent. It is the **evaluation harness and its results table**. Build accordingly.

---

## 1. Problem statement

In a CI system running many test executions, a meaningful share of red builds are not caused by the change under test. They are caused by non-deterministic tests — flaky tests. The costs compound:

- Engineers learn to retry until green, which destroys the signal value of the test suite
- Real regressions get dismissed as "probably flaky"
- Triage time is spent per-engineer, per-incident, and is never captured institutionally

The manual triage task looks like this: an engineer reads a stack trace, checks whether the test has failed before, looks at what changed, and forms a hypothesis about the cause. This is pattern-matching over messy semi-structured text — which is precisely where LLMs are strong, and precisely where deterministic rules are weak.

**What this tool does:** ingests CI test results and code diffs, deterministically identifies which failures are flakes, uses an LLM to classify the *likely cause* of each flake with a suggested remediation, and emits a structured triage report.

**What this tool explicitly does not do:** decide autonomously to disable a test. The LLM proposes; deterministic policy decides. This boundary is a design position and must be documented as such.

---

## 2. Goals and non-goals

### Goals

| ID | Goal |
|---|---|
| G1 | Detect flaky tests deterministically from result history, with no LLM involvement |
| G2 | Classify the likely root cause of a flake into a fixed taxonomy using an LLM |
| G3 | Produce structured, schema-validated output that downstream systems can consume |
| G4 | Measure classification accuracy against a labeled evaluation set |
| G5 | Control cost via a cheap-model prefilter and content-addressed caching |
| G6 | Degrade to an explicit "unknown" rather than to a confident wrong answer |
| G7 | Recommend quarantine decisions via deterministic policy, with LLM input as evidence only |
| G8 | Run as a CLI and as a GitHub Actions step producing a PR comment |

### Non-goals (state these in the README — they are as informative as the goals)

- **Not** a test runner. Consumes results, does not produce them.
- **Not** an auto-fix tool. It does not open PRs that modify test code.
- **Not** a general CI analytics platform. Scope is flake triage only.
- **Not** multi-tenant or authenticated. Single-repo, single-user tool.
- **Not** a real-time service. Batch/invocation model only.

---

## 3. Definitions (implement exactly these)

**Flaky test:** a test that produces different outcomes for the same code state. The primary detection signal is **outcome divergence across executions of the same commit SHA**.

**Regression:** a test that transitions from consistently passing to consistently failing at a specific commit. Not a flake. Must be classified separately.

**New failure:** a test failing on its first observed execution, with no history. Insufficient evidence for either classification. Must be reported as such, never guessed.

**Test identity:** a stable identifier for a logical test across renames and file moves. See §6.2 — this is deliberately non-trivial.

**Flake rate:** proportion of a test's recent execution windows in which same-SHA divergence was observed. Computed over a sliding window, not lifetime.

**Abstention:** the classifier declining to assign a cause. An abstention is a correct behaviour, not a failure, and is measured separately from accuracy.

---

## 4. Cause taxonomy (fixed — the LLM classifies into exactly these)

| Code | Cause | Typical evidence |
|---|---|---|
| `RACE_CONDITION` | Concurrency/ordering nondeterminism within the code under test | Intermittent assertion failures on shared state, thread/executor names in trace |
| `TIMING_DEPENDENCY` | Test depends on wall-clock time, sleeps, or timeouts | `sleep`, timeout exceptions, failures correlated with slow runs |
| `TEST_ORDER_DEPENDENCY` | Test passes alone, fails in a particular suite order | Failure varies with shard assignment; state set by a prior test |
| `EXTERNAL_DEPENDENCY` | Network, third-party service, DNS, container startup | Connection/timeout errors, non-deterministic infrastructure errors |
| `SHARED_STATE_LEAK` | State not reset between tests (DB rows, globals, temp files, caches) | Unique-constraint violations, stale reads, works on rerun after cleanup |
| `RESOURCE_EXHAUSTION` | OOM, file descriptors, port collisions, disk | Killed processes, bind failures, allocation errors |
| `INFRA_FLAKE` | Platform's own fault: worker preemption, image pull failure, runner crash | No test assertion failure present; harness-level error |
| `REAL_REGRESSION` | Genuine defect introduced by the change | Deterministic failure aligned with a diff touching the exercised path |
| `UNKNOWN` | Insufficient or contradictory evidence | Default. Must be used liberally. |

**Design rule:** `INFRA_FLAKE` must never be counted toward a test's flake rate. Attributing platform failures to test authors poisons the metric and destroys trust in the tool. Implement this exclusion explicitly and note it in the README.

---

## 5. Architecture

```
┌──────────────┐   ┌─────────────┐   ┌──────────────┐
│ JUnit XML    │   │ git diff    │   │ run metadata │
│ (results)    │   │ (changes)   │   │ (sha, shard) │
└──────┬───────┘   └──────┬──────┘   └──────┬───────┘
       └──────────────────┼─────────────────┘
                          ▼
                  ┌───────────────┐
                  │  Ingest layer │  parse → normalize → persist
                  └───────┬───────┘
                          ▼
                  ┌───────────────┐
                  │  Run store    │  SQLite (runs, executions, identities)
                  └───────┬───────┘
                          ▼
                  ┌───────────────────────┐
                  │ Deterministic detector│  same-SHA divergence, flake rate,
                  │      (NO LLM)         │  regression vs flake vs new
                  └───────────┬───────────┘
                              ▼
                  ┌───────────────────────┐
                  │   Prefilter / cache   │  content-addressed; skip known
                  └───────────┬───────────┘
                              ▼
                  ┌───────────────────────┐
                  │   LLM classifier      │  structured output + schema
                  │  (proposes cause)     │  validation + abstain path
                  └───────────┬───────────┘
                              ▼
                  ┌───────────────────────┐
                  │  Policy engine        │  DETERMINISTIC quarantine
                  │  (decides)            │  decision; TTL; ownership
                  └───────────┬───────────┘
                              ▼
              ┌───────────────┴───────────────┐
              ▼                               ▼
      ┌───────────────┐              ┌────────────────┐
      │ CLI report    │              │ PR comment /   │
      │ (terminal/JSON)│             │ GH Action out  │
      └───────────────┘              └────────────────┘
```

**Layer boundary rule:** the deterministic detector must be fully functional and testable with the LLM layer disabled (`--no-llm`). This is a hard requirement. It proves the LLM is an enhancement, not a crutch, and it is the first thing a skeptical reviewer will check.

---

## 6. Detailed requirements

### 6.1 Ingest layer

- Parse JUnit XML (the de-facto interchange format across ecosystems: pytest, JUnit, Jest via reporter, Mocha, Playwright)
- Must handle malformed XML gracefully — real CI produces truncated files when workers are killed. Log and skip, never crash the run.
- Use a streaming parser (`iterparse`) — assume result files can be large
- Extract per test case: name, classname/suite, file if available, duration, outcome (pass/fail/error/skip), failure message, stack trace, stdout/stderr capture
- Distinguish `failure` (assertion) from `error` (exception/harness) — this distinction feeds classification
- Ingest git diff: changed files with changed line ranges, from `git diff --unified=0` parsing or a supplied patch file
- Ingest run metadata: commit SHA, branch, run ID, attempt number, shard ID, worker ID, timestamp

### 6.2 Test identity (do not skip this — it is a differentiator)

Naive identity (`suite.Class#method`) breaks on file moves, renames, and parameterized tests. Implement a layered approach:

1. **Primary key:** normalized `(suite_path, test_name)` with parameterization stripped into a separate `parameters` field, so `test_login[user=a]` and `test_login[user=b]` share a logical parent but retain distinct instances
2. **Rename tolerance:** maintain an `identity_alias` table. When a test identity disappears and a new one appears in the same run with a high similarity score (normalized edit distance on name + same file), record an alias and merge history.
3. **Expose the ambiguity:** when an alias is inferred rather than certain, mark the history as `merged_uncertain` and surface it in output. Do not silently merge.

Document in the README why this is harder than it looks. This single section signals real domain experience.

### 6.3 Deterministic detector

Implement, in order of evidence strength:

**Signal 1 — same-SHA divergence (strongest).** For a given `(test_identity, commit_sha)`, if observed outcomes include both pass and fail, this is a confirmed flake. Requires retry data to exist; document that dependency.

**Signal 2 — cross-attempt divergence.** Same test, same SHA, different attempt numbers, differing outcomes. Equivalent to Signal 1 in strength.

**Signal 3 — branch-independent intermittency.** Test fails on a PR that does not touch any file in the test's known execution footprint, and passes on `main` at the same base SHA. Weaker; report confidence accordingly.

**Signal 4 — historical instability.** Flake rate over a sliding window of N executions exceeds a threshold, with a minimum-observations floor. Use an exponentially weighted moving average so recent behaviour dominates; a test fixed last week should not stay condemned by a bad month.

**Regression detection (separate path).** A clean transition from a consistent pass streak to a consistent fail streak, aligned to a specific SHA, is a regression. Emit it as such. Never emit a regression as a flake — this is the most damaging possible error, because it tells an engineer to ignore a real bug.

**Confidence output.** Every detection carries a confidence level (`high` / `medium` / `low`) derived from which signals fired and how many observations back them. Never emit a bare boolean.

### 6.4 LLM classifier

**Input context assembled per flaky test** (keep it tight — context bloat is a cost and accuracy problem):
- Failure message and stack trace, truncated intelligently (keep the head and the frames belonging to project code, drop framework noise)
- Test source snippet if resolvable from the file path
- The relevant slice of the git diff — only hunks touching files that appear in the stack trace
- Compact execution history summary: recent outcomes, flake rate, whether it fails only in specific shards
- Detector output: which signals fired

**Output contract:**
- Structured JSON only, validated against a strict schema (Pydantic model)
- Fields: `cause` (from the fixed taxonomy), `confidence` (0.0–1.0), `reasoning` (≤ 3 sentences), `evidence` (list of concrete quoted-or-referenced observations from the input), `suggested_action` (free text, ≤ 2 sentences), `abstained` (bool)
- **Schema validation failure must not crash.** Retry once with a repair prompt; on second failure, emit `UNKNOWN` with `abstained=true` and log the malformed output for inspection.

**Abstention requirements:**
- The prompt must explicitly instruct that `UNKNOWN` is a correct and expected answer when evidence is thin
- Any classification below a configurable confidence floor is downgraded to `UNKNOWN`
- `evidence` must be non-empty for any non-`UNKNOWN` classification. If the model returns a cause with no evidence, downgrade it. This is a cheap, effective hallucination guardrail.

**Cost controls (all must be implemented and measured):**
- **Content-addressed cache:** key on a hash of the assembled prompt context. Identical failures across runs cost nothing. Report cache hit rate.
- **Cheap-model prefilter:** a small/fast model performs a binary triage — "is there enough signal here to classify at all?" Only positives escalate to the larger model. Measure what fraction is filtered and what accuracy is lost.
- **Hard budget cap:** a per-invocation token/cost ceiling. On exhaustion, remaining items are emitted as `UNKNOWN` with reason `budget_exhausted` — never silently truncated.
- **Batch bounding:** cap the number of tests classified per invocation; prioritize by flake rate × recency.

**Determinism:** temperature 0 for classification. Record the model identifier and prompt version hash in every output record so results are reproducible and regressions in prompt quality are attributable.

### 6.5 Evaluation harness — **the centerpiece**

Without this, the project is indistinguishable from every other LLM demo. Build it properly.

**Labeled dataset:**
- Minimum 40 examples, target 60, spread across all taxonomy classes including `UNKNOWN`
- Each example: input context + ground-truth cause label + a one-line human rationale
- Store as versioned JSON/YAML under `eval/dataset/`
- **Include adversarial cases deliberately:** a real regression that superficially looks like a flake; an infra failure that looks like a race condition; a case with genuinely insufficient evidence where `UNKNOWN` is the correct label. Document that you constructed these on purpose.

**Dataset construction:** you will need to synthesize most of these. That is acceptable and must be disclosed honestly in the README — write a generator (`eval/generate_corpus.py`) that produces realistic JUnit XML and stack traces for each cause category, and hand-label. State plainly: "this corpus is synthetic; accuracy figures are indicative, not production-validated." **Honest limitation statements read as strength to a senior reviewer; overclaiming reads as inexperience.**

**Metrics reported:**
- Per-class precision and recall (not just overall accuracy — the classes are imbalanced)
- Confusion matrix
- Abstention rate, and accuracy conditional on not abstaining
- **Dangerous-error rate:** the rate of `REAL_REGRESSION` misclassified as any flake category. Track and report this separately and prominently — it is the error that actually costs money.
- Mean cost per classification and P50/P95 latency
- Cache hit rate

**Deliverable:** `make eval` runs the harness and writes `eval/results/latest.md` containing the metrics table. This file is committed and referenced from the README.

**Baseline comparison:** implement a trivial keyword-heuristic classifier (`ConnectionError` → `EXTERNAL_DEPENDENCY`, `timeout` → `TIMING_DEPENDENCY`, etc.) and report its scores alongside the LLM's. If the LLM does not beat the baseline on some classes, **report that too**. Knowing where the simple thing wins is a senior signal.

### 6.6 Policy engine (quarantine recommendation)

Deterministic only. The LLM's classification is *input evidence*, never the decision.

Recommend quarantine when **all** hold:
- Flake rate exceeds threshold (default 5%) over the window
- Minimum observation count met (default 10 executions)
- Cause is not `REAL_REGRESSION` and not `INFRA_FLAKE`
- Test is not already quarantined

Every quarantine recommendation must include:
- **A TTL** (default 14 days). Quarantine without expiry becomes a graveyard of permanently disabled tests. Implement expiry and surface expiring items in the report.
- **An owner**, resolved from `CODEOWNERS` where available, else the last committer to the test file
- **A de-quarantine condition:** N consecutive clean executions returns the test to blocking status automatically

Also implement: a quarantined test **continues to execute**, it merely stops blocking. Without continued execution you have no data to de-quarantine on. State this in the README — it is a design decision reviewers will probe.

### 6.7 Interfaces

**CLI (Typer):**
```
flaketriage ingest --results ./reports/*.xml --sha <SHA> --run-id <ID> [--attempt N]
flaketriage detect [--since 30d] [--json]
flaketriage triage --sha <SHA> [--no-llm] [--budget-usd 0.50] [--max-tests 25]
flaketriage report --format {terminal,json,markdown}
flaketriage policy --show-quarantine [--expiring]
flaketriage eval [--subset <class>]
```

**GitHub Actions:** provide `action.yml` plus an example workflow in `.github/workflows/example-triage.yml` that runs on test failure and posts a markdown comment to the PR. The comment must be concise: a short table of flaky failures with causes and confidence, an explicit "these failures appear unrelated to your change" summary line, and a collapsed `<details>` block for full reasoning. **Verbose bot comments get muted; brevity is a functional requirement, not a stylistic one.**

### 6.8 Observability (leverage this — it differentiates the repo)

- Structured JSON logging throughout (`structlog`)
- Emit run metrics to a local SQLite table and expose `flaketriage stats`: classifications by cause, abstention rate, cost per run, cache hit rate, LLM latency percentiles
- Optional Prometheus text-format output (`--metrics-out`) so the tool could be scraped in a real deployment
- Every LLM call logged with: prompt hash, model, token counts, cost, latency, cache hit/miss, schema-validation outcome

---

## 7. Technical stack

| Concern | Choice | Rationale (put this in the README) |
|---|---|---|
| Language | Python 3.12+ | Primary language; strong XML/data tooling |
| Package/env | `uv` | Fast, lockfile-based, reproducible |
| CLI | `typer` | Type-driven, minimal boilerplate |
| Validation | `pydantic` v2 | Schema enforcement for LLM output is the core guardrail |
| Storage | SQLite | Zero-ops, portable, sufficient. Schema designed so it could move to a columnar store — note the migration path. |
| LLM | `anthropic` SDK | Structured output; two-tier model strategy |
| Logging | `structlog` | JSON logs |
| Testing | `pytest`, `pytest-cov`, `hypothesis` | Property-based tests on the detector are a strong signal |
| Quality | `ruff`, `mypy --strict` | Enforced in CI |
| CI | GitHub Actions | Lint, type-check, test, and run eval on a cached subset |

**Configuration:** `pyproject.toml` for tooling; a `flaketriage.toml` for thresholds (flake rate, window size, TTL, budget). No magic numbers in code.

**Secrets:** API key from environment only. Never read from a config file. `.env.example` provided, `.env` gitignored.

---

## 8. Repository layout

```
flaky-triage-agent/
├── README.md                      # design doc — see §10
├── ARCHITECTURE.md                # diagrams + data flow
├── docs/
│   └── adr/
│       ├── 0001-deterministic-core-llm-advisory.md
│       ├── 0002-test-identity-strategy.md
│       ├── 0003-abstention-over-guessing.md
│       ├── 0004-quarantine-ttl.md
│       └── 0005-two-tier-model-cost-strategy.md
├── src/flaketriage/
│   ├── ingest/       # junit parser, diff parser, metadata
│   ├── store/        # sqlite schema, migrations, repositories
│   ├── detect/       # deterministic signals, flake rate, regression
│   ├── identity/     # fingerprinting, alias resolution
│   ├── classify/     # prompts, client, schema, cache, prefilter, budget
│   ├── policy/       # quarantine rules, ttl, ownership
│   ├── report/       # terminal, json, markdown/PR renderers
│   ├── obs/          # logging, metrics
│   └── cli.py
├── eval/
│   ├── dataset/                   # labeled examples
│   ├── generate_corpus.py         # synthetic corpus generator
│   ├── baseline.py                # keyword heuristic
│   ├── run_eval.py
│   └── results/latest.md          # committed
├── tests/
├── action.yml
├── .github/workflows/
├── flaketriage.toml
├── Makefile
└── pyproject.toml
```

---

## 9. Build phases

Implement strictly in order. Each phase must be committed working, with tests, before the next begins.

| Phase | Deliverable | Done when |
|---|---|---|
| **P0** | Scaffolding: `uv` project, ruff/mypy/pytest configured, Makefile, CI workflow, empty CLI | `make check` passes on an empty project |
| **P1** | Ingest: JUnit XML parser, diff parser, SQLite schema, `ingest` command | Malformed XML handled without crash; tests cover 4 result formats |
| **P2** | Identity: fingerprinting, parameterization handling, alias resolution | Property-based test: renaming a test preserves history |
| **P3** | Detector: all four signals, regression path, confidence levels, `detect --no-llm` | Full pipeline works end-to-end with **zero LLM calls** |
| **P4** | Classifier: prompt, schema, validation, repair retry, abstention, cache | Malformed model output produces `UNKNOWN`, never an exception |
| **P5** | **Eval harness**: corpus generator, ≥40 labeled examples, baseline, metrics, `eval/results/latest.md` | `make eval` produces the committed results table |
| **P6** | Cost controls: prefilter, budget cap, cost accounting in `stats` | Budget exhaustion produces graceful `UNKNOWN`, measured cache hit rate reported |
| **P7** | Policy engine: quarantine rules, TTL, ownership, de-quarantine | Expiry surfaces in report; regression never recommended for quarantine |
| **P8** | Interfaces: markdown/PR renderer, `action.yml`, example workflow | Action runs in CI on the repo's own tests |
| **P9** | Docs: README as design doc, ARCHITECTURE, 5 ADRs, demo output | A reviewer can understand the trade-offs without reading code |

**If time is short, the minimum defensible version is P0–P5.** A repository with a working deterministic detector and a real evaluation table beats a feature-complete one with no measurement. Do not sacrifice P5.

---

## 10. README requirements (treat as a design doc under review)

The README is the primary artifact a reviewer reads. Structure:

1. **The problem, in three sentences**, with the cost framed in engineer-hours
2. **What it does** — one diagram, one example of real output
3. **Design position: deterministic core, LLM advisory.** State it explicitly and justify it. This is the intellectual spine of the project.
4. **Results table** — pull from `eval/results/latest.md`. Include the dangerous-error rate and the baseline comparison. Include the numbers that are unflattering.
5. **Cost and latency** — measured, per classification, with the cache hit rate
6. **Limitations, stated plainly:**
   - Evaluation corpus is synthetic; figures are indicative
   - Detection requires retry data to exist in CI
   - Test identity aliasing is heuristic and can merge incorrectly
   - Single-repo scope; no multi-tenancy or auth
   - Not validated against a production test suite
7. **What I would do differently / next** — three concrete items with reasoning
8. **Quickstart** — working commands, under five lines

**Tone:** measured and specific. No "revolutionary", no "AI-powered" as a selling point, no emoji. The credibility comes from the limitations section being longer than the features section.

---

## 11. Acceptance criteria

- [ ] `make check` — ruff clean, `mypy --strict` clean, tests pass
- [ ] `flaketriage triage --no-llm` produces a complete, useful report with zero API calls
- [ ] Malformed LLM output never raises; always degrades to `UNKNOWN`
- [ ] Budget exhaustion degrades gracefully with an explicit reason
- [ ] `make eval` regenerates `eval/results/latest.md` with per-class precision/recall, confusion matrix, abstention rate, dangerous-error rate, cost, latency, and baseline comparison
- [ ] A `REAL_REGRESSION` is never recommended for quarantine
- [ ] `INFRA_FLAKE` is excluded from flake-rate computation
- [ ] Quarantine recommendations always carry a TTL and an owner
- [ ] Test coverage ≥ 80% on `detect/`, `identity/`, and `policy/` (the deterministic core)
- [ ] Five ADRs written
- [ ] README contains a limitations section with at least five honest entries

---

## 12. Questions a reviewer will ask — make sure the code answers them

Build so that each of these has a concrete answer in the repository:

1. *"What happens when the model returns garbage?"* → schema validation, repair retry, `UNKNOWN` fallback, logged sample
2. *"How do you know it works?"* → `eval/results/latest.md`, with a baseline it must beat
3. *"What does it cost to run?"* → measured per-classification cost, cache hit rate, prefilter savings
4. *"What's the worst thing it can do?"* → misclassify a regression as a flake; tracked as a first-class metric and mitigated by never letting the LLM make the quarantine decision
5. *"Why not just use regex on stack traces?"* → the baseline classifier is implemented, and the results show exactly where it wins and where it loses
6. *"How do you identify a test across a rename?"* → §6.2, ADR-0002, property-based test
7. *"Would you deploy this?"* → the README limitations section answers this honestly: not without validation against a real suite and a shadow-mode period where recommendations are logged but not acted on

---

## 13. Explicit anti-requirements

Do **not** build any of the following. If tempted, note it in "future work" instead:

- Chat interface or conversational agent loop
- Automatic PR creation that modifies test code
- Web dashboard or UI
- Multi-model ensemble or voting
- Fine-tuning or embedding-based retrieval — unnecessary complexity for a classification task over short contexts, and would need to be justified with data this project won't have
- Any autonomous action that changes CI state without human confirmation

Scope discipline is itself a signal. A tight repository that does one thing with measured quality is stronger than a broad one that does five things unmeasured.
