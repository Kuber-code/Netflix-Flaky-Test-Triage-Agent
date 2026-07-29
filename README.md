# flaketriage

Deterministic flaky-test detection for CI, with an LLM used only to propose a
likely cause. The detector works with the model switched off; the model never
makes a decision that changes CI state.

> **Build status: in progress.** Phases are implemented in the order given in
> [the build specification](flaky-triage-agent-REQUIREMENTS.md#9-build-phases).
> See [Build status](#build-status) for what currently exists. Sections that
> depend on measurement are marked as pending rather than filled with estimates.

## The problem

In a CI system running many test executions, a meaningful share of red builds
are not caused by the change under test — they are caused by non-deterministic
tests. Engineers learn to retry until green, which destroys the signal value of
the suite, and real regressions get waved through as "probably flaky". The
triage work itself is repeated per-engineer and per-incident and is never
captured institutionally, so the same stack trace is diagnosed from scratch
every time it appears.

## What it does

```
JUnit XML + git diff + run metadata
              |
              v
   ingest -> run store (SQLite)
              |
              v
   deterministic detector  <- no LLM, ever
     same-commit divergence, cross-attempt divergence,
     branch-independent intermittency, EWMA flake rate,
     separate regression path, confidence levels
              |
              v
   prefilter / content-addressed cache        (P6)
              |
              v
   LLM classifier: proposes a cause           (P4)
     strict schema, repair retry, abstain path
              |
              v
   policy engine: DETERMINISTIC quarantine    (P7)
     decision, TTL, ownership
              |
      +-------+-------+
      v               v
   CLI report      PR comment
```

Real output from `flaketriage detect` over eleven ingested runs of a four-test
suite:

```
verdict     conf    rate  obs  test
regression  high      0%   11  tests/integration/test_checkout.py::test_totals_include_tax
flaky       medium   30%   10  tests/integration/test_checkout.py::test_payment_capture
flaky       medium   21%   11  tests/integration/test_checkout.py::test_concurrent_checkout

tests/integration/test_checkout.py::test_payment_capture
  - same_sha_divergence: 1 commit(s) produced both a pass and a failure; most recent: b91f4a2d7e0c
  - cross_attempt_divergence: outcome differed between attempts at b91f4a2d7e0c (attempts 1, 2)
  - historical_instability: flake rate 30.0% over 10 observations exceeds 5.0%,
    measured from same-commit divergence

2 flaky, 1 regression, 0 persistent, 0 new, 1 healthy
```

Three things in that output are the point of the project. The regressing test is
called a **regression, not a flake**, even though it started failing partway
through the window. `test_payment_capture` shows **10 observations where the
others show 11** — one of its failures was a runner disk-full error, excluded from
the flake rate entirely. And every row carries a **confidence level**, because
"diverged at one commit yesterday" and "the flip rate crept over five percent"
are both "flaky" and are not the same claim.

## Design position: deterministic core, LLM advisory

This is the intellectual spine of the project and the reason for the package
layout. Recorded in full in
[ADR-0001](docs/adr/0001-deterministic-core-llm-advisory.md).

- Whether a test **is** flaky is a question about observed outcomes. It is
  answered by counting, not by inference: if the same commit SHA produced both a
  pass and a fail, that is a confirmed flake regardless of what any model says.
- Why a test is flaky is pattern-matching over messy semi-structured text, which
  is where a model earns its keep and where regex does not.
- What to **do** about it — quarantine or not — is a policy decision with real
  cost when it is wrong. It stays deterministic, takes the model's
  classification as one input among several, and is never delegated.

Consequences that are enforced in code rather than asserted in prose:

- `flaketriage triage --no-llm` produces a complete report with zero API calls,
  and the deterministic packages do not import the classifier package.
- A classification below the configured confidence floor, or one with no
  supporting evidence, is downgraded to `UNKNOWN`. Abstention is a correct
  answer, measured separately from accuracy.
- `INFRA_FLAKE` — the platform's own fault — never counts toward a test's flake
  rate. Attributing runner preemptions to test authors poisons the metric and
  destroys trust in the tool.
- A `REAL_REGRESSION` is never recommended for quarantine. Telling an engineer
  to ignore a genuine bug is the most expensive error this tool can make, so it
  is tracked as a first-class metric.

## Non-goals

- **Not** a test runner. It consumes results; it does not produce them.
- **Not** an auto-fix tool. It does not open PRs that modify test code.
- **Not** a CI analytics platform. Scope is flake triage only.
- **Not** multi-tenant or authenticated. Single repo, single user.
- **Not** a service. Batch invocation only.

## Quickstart

```bash
make install                       # uv sync --all-groups
make check                         # ruff, mypy --strict, pytest
uv run flaketriage ingest --results ./reports --sha "$SHA" --run-id "$RUN_ID" --attempt 1
uv run flaketriage triage --no-llm            # complete report, zero API calls
```

On Windows, where GNU make is not present, `.\make.ps1 check` runs the same gate.

`ingest` accepts files, directories or globs — globs are expanded in-process
because PowerShell and cmd do not expand them. Re-ingesting the same run,
attempt and shard is a no-op rather than a duplicate: CI retries ingest steps,
and double-counting observations would corrupt every flake rate downstream.

### What the ingest layer handles

Five JUnit dialects are covered by fixtures and tests — pytest, Maven Surefire,
jest-junit, Playwright, and reporters that nest `<testsuite>` elements to model
describe blocks. "JUnit XML" is a family of dialects rather than a schema, so
each is exercised separately:

- `failure` and `error` are kept distinct, because an assertion failure points at
  the code under test while a harness error more often points at infrastructure.
- Surefire's `<flakyFailure>` is recorded as a rerun observation: same-SHA
  outcome divergence asserted by the test runner itself.
- Truncated XML — what a killed worker leaves behind — yields the cases parsed
  before the truncation point plus a persisted warning. Losing a whole run's
  results because the closing tag is missing is strictly worse than partial data.
- A `DOCTYPE` is refused outright. It has no legitimate purpose in a CI artifact
  and is the vector for entity-expansion attacks.

### Test identity is harder than it looks

Every number this tool produces is computed over a test's history, so history is
only as good as the key it hangs on — and the obvious key breaks in four ordinary
situations, each of which silently resets history to zero: parameterized tests,
reporters disagreeing on how to name the same test, renames, and file moves. The
damaging property is shared: history resets exactly when an engineer is touching
a flaky test, which is when its history is most needed.

The key is a normalized, hashed `(suite_path, test_name, parameters)` triple.
Renames and moves are reconciled through an alias table: an identity that
produces no execution in a run, paired with a previously unseen identity in the
same run, at a combined name-and-path distance within threshold, and only when
the pairing is unambiguous. Two disappeared tests competing for one appeared test
merge nothing.

Only typo-level distances are recorded as certain. Everything else is stored as
`merged_uncertain` and surfaced in output, because a wrongly merged history
produces a flake rate that describes no real test and the reader would otherwise
have no way to tell. A test renamed *and* moved in one commit loses its history
on purpose — both signals changed, so there is no evidence distinguishing it from
an unrelated deletion plus addition. See
[ADR-0002](docs/adr/0002-test-identity-strategy.md).

### How the flake rate is computed

Two decisions here are worth stating because both are easy to get wrong in a way
that looks more rigorous than it is.

**The denominator is every commit, not every retried commit.** Restricting it to
commits that *could* show divergence sounds correct — a single observation cannot
disagree with itself — and is badly wrong in practice. Real pipelines retry only
on failure, so every retried commit is one that already failed, and a large share
of them diverge. The rate then reads near 100% for a test that fails one run in
ten. Retries determine whether divergence is *observable*; they do not determine
what to divide by.

**The rate is exponentially weighted, not a window mean.** A test fixed last week
should not stay condemned by a bad month. With `ewma_alpha = 0.3`, each clean run
multiplies the estimate by 0.7, so ten green runs take a fully-red history under
3%. A 50-run window mean would still be reporting 60%.

When a pipeline never retries, no same-commit evidence exists at all, and the rate
falls back to counting outcome flips between consecutive commits. That measure is
weaker — a test that was genuinely fixed also flips — so detections resting on it
are reported at low confidence, and `retry_data_available: false` appears in the
JSON output rather than being averaged into one indistinguishable number.

## Build status

| Phase | Deliverable | Status |
|---|---|---|
| P0 | Scaffolding: uv project, ruff/mypy/pytest, Makefile, CI, CLI surface | done |
| P1 | Ingest: JUnit XML, diff parser, SQLite schema, `ingest` | done |
| P2 | Identity: alias resolution across renames and moves | done |
| P3 | Detector: four signals, regression path, confidence, `detect`/`report` | done |
| P4 | Classifier: schema validation, repair retry, abstention, cache | pending |
| P5 | Evaluation harness, labeled corpus, baseline, results table | pending |
| P6 | Cost controls: prefilter, budget cap, cost accounting | pending |
| P7 | Policy engine: quarantine rules, TTL, ownership, de-quarantine | pending |
| P8 | Interfaces: PR comment renderer, `action.yml` | pending |
| P9 | Docs: README as design doc, ARCHITECTURE, ADRs | pending |

Unimplemented CLI commands exit non-zero with an explicit message. They do not
exit 0 and return nothing, because a missing feature that looks like an empty
result is worse than a missing feature.

## Results

Pending phase P5. This section will contain per-class precision and recall, a
confusion matrix, the abstention rate, the dangerous-error rate
(`REAL_REGRESSION` classified as any flake category), measured cost and latency,
cache hit rate, and a comparison against a keyword-heuristic baseline — pulled
from `eval/results/latest.md`, including the unflattering numbers.

## Limitations

Stated up front because they bound what any results figure can mean.

1. The evaluation corpus will be **synthetic**. Accuracy figures will be
   indicative of behaviour on realistic-looking inputs, not production-validated.
2. Detection depends on **retry data existing** in CI. The strongest signal is
   outcome divergence within one commit SHA; a pipeline that never retries
   cannot produce it, and the tool falls back to weaker, lower-confidence
   signals.
3. Test identity aliasing across renames is **heuristic** and can merge two
   distinct tests. Uncertain merges are marked rather than hidden, but they are
   still merges.
4. Single-repo scope. No multi-tenancy, no auth, no access control.
5. Not validated against a production test suite. Deploying it would warrant a
   shadow-mode period in which recommendations are logged and not acted on.

## Configuration

Thresholds live in [flaketriage.toml](flaketriage.toml) — flake-rate threshold,
window size, quarantine TTL, budget ceiling, confidence floor. There are no
behavioural constants in the source. The Anthropic API key is read from the
environment only; there is deliberately no config field for it, so it cannot be
committed by accident. See [.env.example](.env.example).

## License

MIT.
