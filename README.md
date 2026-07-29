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

## Design position: deterministic core, LLM advisory

This is the intellectual spine of the project and the reason for the package
layout.

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

## Build status

| Phase | Deliverable | Status |
|---|---|---|
| P0 | Scaffolding: uv project, ruff/mypy/pytest, Makefile, CI, CLI surface | done |
| P1 | Ingest: JUnit XML, diff parser, SQLite schema, `ingest` | done |
| P2 | Identity: alias resolution across renames | pending |
| P3 | Detector: four signals, regression path, confidence levels | pending |
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
