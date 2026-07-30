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
   prefilter / content-addressed cache
              |
              v
   LLM classifier: proposes a cause
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

### Abstention is a correct answer

`UNKNOWN` is a first-class member of the taxonomy, and four mechanisms route to
it: the prompt says so explicitly, evidence is mandatory for any non-`UNKNOWN`
cause, classifications below a configurable confidence floor are downgraded, and
every failure mode — malformed JSON, an invented cause code, an API outage, an
exhausted budget — ends in `UNKNOWN` with a distinct machine-readable reason
rather than an exception.

The reasoning is specific to how the output is used. In the table an engineer
reads while deciding whether to investigate a red build, a wrong
`RACE_CONDITION` and a right one look identical. `UNKNOWN` costs a reader a few
seconds; a plausible wrong cause costs them the investigation they would otherwise
have done. See [ADR-0003](docs/adr/0003-abstention-over-guessing.md), which also
records what this cost in practice — the first prefilter prompt silently rejected
a `ConnectionResetError` with a full stack trace.

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
| P4 | Classifier: schema validation, repair retry, abstention, cache, prefilter | done |
| P5 | Evaluation harness, labeled corpus, baseline, results table | done |
| P6 | Cost controls: prefilter, budget cap, cost accounting | done in P4 |
| P7 | Policy engine: quarantine rules, TTL, ownership, de-quarantine | pending |
| P8 | Interfaces: PR comment renderer, `action.yml` | pending |
| P9 | Docs: README as design doc, ARCHITECTURE, ADRs | pending |

Unimplemented CLI commands exit non-zero with an explicit message. They do not
exit 0 and return nothing, because a missing feature that looks like an empty
result is worse than a missing feature.

## Cost and latency

Measured against the real API on a two-test corpus, cold cache:

| call | model | input tok | output tok | cost | latency |
|---|---|---|---|---|---|
| prefilter | `claude-haiku-4-5` | 539 | 8 | $0.00058 | 1.1s |
| classify | `claude-sonnet-5` | 2498 | 362 | $0.01292 | 4.2s |

**~$0.0134 per classified test** cold; **$0.00 warm** — a second identical
invocation was a 100% cache hit. The input side of the classify call dominates and
most of it is the system prompt carrying the taxonomy, so context assembly is the
cost lever rather than output length. Prompt caching for that stable block is the
obvious next step and is deliberately unbuilt until P5 can measure the saving.

Three constraints found by calling the API rather than by assuming, all recorded in
[ADR-0005](docs/adr/0005-two-tier-model-cost-strategy.md):

- **`claude-sonnet-5` rejects `temperature`** with a 400. The client drops it and
  retries, remembering the model. So the spec's "temperature 0 for reproducibility"
  is not fully available on the configured classifier; attributability comes from
  the recorded model id and prompt-version hash instead.
- **The structured-output schema dialect is a subset of JSON Schema** — `minimum`
  and `maximum` on a number are rejected. That is the concrete reason the Pydantic
  validation layer is not redundant with the API's own enforcement.
- **The first content block is not necessarily text.** A reasoning model emits a
  thinking block first, so `content[0].text` is a latent crash.

## Results

Full table in [eval/results/latest.md](eval/results/latest.md), regenerated by
`make eval`. 49 hand-labelled examples across all nine taxonomy classes, 13 of them
constructed adversarially.

| metric | keyword baseline | LLM classifier |
|---|---|---|
| **dangerous-error rate** (`REAL_REGRESSION` called a flake) | **20.0%** | **0.0%** |
| overall accuracy | 77.6% | 91.8% |
| macro F1 | 0.756 | 0.915 |
| abstention rate | 32.7% | 12.2% |
| accuracy when answering | 93.9% | **90.7%** |
| accuracy on adversarial cases | 46.2% | 84.6% |

The dangerous-error rate is listed first because it is the only metric here with an
asymmetric cost. Every other error wastes a reader's time; this one tells an
engineer to ignore a real bug. The baseline scores 20% on it for a structural
reason, not a tuning one: no keyword distinguishes "this failure is a real defect"
from "this failure is noise", because that judgement needs the history and the
diff. That gap is the entire justification for the LLM layer.

Note the one place the baseline is ahead in the headline table: **accuracy when
answering**, 93.9% against 90.7%. The baseline abstains on a third of the corpus
and is more often right on the rest. Reporting that number without its companion
abstention rate would be the easiest available way to mislead with this table,
which is why both are always shown together.

### Where the baseline wins — and it does

| class | baseline F1 | LLM F1 |
|---|---|---|
| `INFRA_FLAKE` | **1.00** | 0.75 |
| `RESOURCE_EXHAUSTION` | **1.00** | 0.80 |
| `EXTERNAL_DEPENDENCY` | 1.00 | 1.00 |
| `TEST_ORDER_DEPENDENCY` | 1.00 | 1.00 |
| `UNKNOWN` (recall) | **88%** | 75% |

The LLM recalls only 60% of `INFRA_FLAKE` cases where a keyword list gets all five.
That is worth more than the aggregate win, and it points somewhere concrete: infra
detection should stay deterministic, which is exactly where it already lives — the
detector excludes platform failures from the flake rate by pattern before any model
runs. Four classes are matched by a regex, so on those the model is not earning its
cost.

### Measured cost and latency

Cold run over the full corpus: 99 API calls, **$0.6860 total, $0.0140 per
example**, P50 5.1s and P95 9.1s per classification. An immediate re-run is a
**100% cache hit at $0.00**. The repair retry fired and recovered 3 malformed
responses. 2 example(s) were stopped by the cheap-model gate.

### What these numbers do not show

- The corpus is **synthetic and hand-labelled**. Real traces are longer, noisier
  and more often genuinely ambiguous; hand-written examples are cleaner than what
  CI actually produces.
- The corpus author also wrote the prompt, so some of the LLM's advantage may be
  shared vocabulary rather than shared reasoning.
- 49 examples is small. One reclassified example moves overall accuracy about two
  points, so differences of a few points are noise.

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
