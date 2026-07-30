# flaketriage

Deterministic flaky-test detection for CI, with an LLM used only to propose a
likely cause. The detector works with the model switched off; the model never
makes a decision that changes CI state.

All nine build phases of [the specification](flaky-triage-agent-REQUIREMENTS.md)
are implemented. Structure and data flow are in
[ARCHITECTURE.md](ARCHITECTURE.md); the reasoning behind each contentious choice
is in the five [ADRs](docs/adr/).

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

A full transcript of one pipeline run -- ingest, detect, triage, policy, stats --
is in [docs/demo.md](docs/demo.md). Real output from `flaketriage detect` over
twelve ingested runs of a four-test suite:

```
verdict     conf    rate  obs  test
regression  high      0%   12  tests/integration/test_checkout.py::test_totals_include_tax
flaky       medium   30%   11  tests/integration/test_checkout.py::test_payment_capture
flaky       medium   21%   12  tests/integration/test_checkout.py::test_concurrent_checkout

tests/integration/test_checkout.py::test_payment_capture
  - same_sha_divergence: 1 commit(s) produced both a pass and a failure; most recent: b91f4a2d7e0c
  - cross_attempt_divergence: outcome differed between attempts at b91f4a2d7e0c (attempts 1, 2)
  - historical_instability: flake rate 30.0% over 11 observations exceeds 5.0%,
    measured from same-commit divergence

2 flaky, 1 regression, 0 persistent, 0 new, 1 healthy
```

Three things in that output are the point of the project. The regressing test is
called a **regression, not a flake**, even though it started failing partway
through the window. `test_payment_capture` shows **11 observations where the
others show 12** — one of its failures was a runner disk-full error, excluded from
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
`make cov` additionally enforces the specification's 80% coverage floor on
`detect/`, `identity/` and `policy/` per package — a global threshold would let the
deterministic core rot while the total stayed healthy, so the criterion is a gate
rather than a claim.

`ingest` accepts files, directories or globs — globs are expanded in-process
because PowerShell and cmd do not expand them. Re-ingesting the same run,
attempt and shard is a no-op rather than a duplicate: CI retries ingest steps,
and double-counting observations would corrupt every flake rate downstream.

## How it works

The four sections below are the parts where the obvious implementation is wrong in
a way that only shows up later.

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

### Quarantine is where an LLM triage tool usually starts doing harm

A test is flaky, so it gets quarantined to unblock the pipeline. The quarantine has
no expiry, nobody owns it, and there is no route back. Two years later the suite has
three hundred quarantined tests and the ones that were catching real bugs have been
silently not catching them the whole time. The quarantine list has become a deletion
list nobody had to argue for.

Everything in the policy engine exists to make that outcome structurally awkward:

- **Four conditions, all required**, and any failure is reported as a named refusal
  reason rather than a silent absence — "why not this one?" is a question the output
  should already answer.
- **The model can veto but never cause.** A model that hallucinated
  `RACE_CONDITION` on every test in the suite could not quarantine anything the
  flake rate had not already condemned. There is a test asserting exactly that.
- **The regression check consults both layers** — detector verdict and model cause
  — and either one saying "regression" refuses. They can disagree, and requiring
  agreement would let a defect through whenever one was wrong.
- **Expired and released are different states.** Released means the test earned its
  way back; expired means the TTL ran out while it was still unstable and a human
  has to decide. Collapsing them hides the only distinction a reader cares about.
- **Owner provenance is labelled.** This repository has its own
  [`.github/CODEOWNERS`](.github/CODEOWNERS), which is both real ownership metadata
  and the input this path reads. `CODEOWNERS` is a statement of responsibility;
  the last committer is a guess that happens to be usually right. Showing them
  identically would let a guess inherit the authority of a declaration.
- **A quarantined test keeps running.** It stops blocking, not executing — the exit
  condition is N consecutive clean runs, so a test that stops running can never
  satisfy it. Nothing in the schema records a test as disabled, because the tool
  never disables one.

`flaketriage policy` is read-only by default; `--apply` writes to this tool's own
records and nothing else. See [ADR-0004](docs/adr/0004-quarantine-ttl.md).

### Observability

`flaketriage stats` aggregates over persisted model calls and classifications:
cost per classification, cache hit rate, abstention rate broken down **by reason**,
classifications by cause, and P50/P95 classify latency. `--metrics-out` writes
Prometheus text format so the tool could be scraped rather than only read.

Two decisions there are worth naming. Abstentions are stored rather than skipped —
an abstention rate cannot be computed from a table that only records the times the
classifier had something to say. And the cache hit rate divides by classification
*attempts*, not by API calls: dividing by calls would make the rate rise as the
cache got worse, because every miss adds to the denominator and every hit adds
nothing.

## Build status

Every phase is done. The table is kept because the order was load-bearing: each
phase had to be committed working, with tests, before the next began, and several
of the more useful findings came from a later phase exercising an earlier one.

| Phase | Deliverable | Status |
|---|---|---|
| P0 | Scaffolding: uv project, ruff/mypy/pytest, Makefile, CI, CLI surface | done |
| P1 | Ingest: JUnit XML, diff parser, SQLite schema, `ingest` | done |
| P2 | Identity: alias resolution across renames and moves | done |
| P3 | Detector: four signals, regression path, confidence, `detect`/`report` | done |
| P4 | Classifier: schema validation, repair retry, abstention, cache, prefilter | done |
| P5 | Evaluation harness, labeled corpus, baseline, results table | done |
| P6 | Cost controls and observability: `stats`, Prometheus output | done |
| P7 | Policy engine: quarantine rules, TTL, ownership, de-quarantine | done |
| P8 | Interfaces: PR comment renderer, `action.yml`, example workflow | done |
| P9 | Docs: README as design doc, ARCHITECTURE, five ADRs | done |

Design records:

| ADR | Decision |
|---|---|
| [0001](docs/adr/0001-deterministic-core-llm-advisory.md) | Deterministic core, LLM advisory |
| [0002](docs/adr/0002-test-identity-strategy.md) | Test identity and explicit, labelled aliasing |
| [0003](docs/adr/0003-abstention-over-guessing.md) | Abstention over guessing |
| [0004](docs/adr/0004-quarantine-ttl.md) | Quarantine carries a TTL, an owner, and an exit condition |
| [0005](docs/adr/0005-two-tier-model-cost-strategy.md) | Two-tier model strategy and cost controls |

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

### What these numbers do not show

- The corpus is **synthetic and hand-labelled**. Real traces are longer, noisier
  and more often genuinely ambiguous; hand-written examples are cleaner than what
  CI actually produces.
- The corpus author also wrote the prompt, so some of the LLM's advantage may be
  shared vocabulary rather than shared reasoning.
- 49 examples is small. One reclassified example moves overall accuracy about two
  points, so differences of a few points are noise.

## GitHub Action

```yaml
- uses: Kuber-code/Netflix-Flaky-Test-Triage-Agent@main
  with:
    results: reports
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}   # optional
```

`results` is the only required input. Omit the key and the deterministic detector
still produces a complete report — only the proposed cause is missing.

Details that matter more than the YAML:

- **The comment is updated, not re-posted.** A hidden marker identifies the
  action's own comment and the upsert edits it. A bot that posts a fresh comment on
  every push gets muted, and a muted bot has no effect on anything — which is why
  brevity here is a functional requirement rather than a stylistic one.
- **`attempt` defaults to `github.run_attempt`**, not to 1. A pipeline that reports
  every retry as attempt 1 throws away the strongest flake signal there is.
- **It reports; it does not gate.** `fail-on-regression` exists and defaults to
  off. Turning triage into a required check should be a team's deliberate decision
  on their own evidence.
- **It runs on this repository's own tests** via
  [`.github/workflows/self-triage.yml`](.github/workflows/self-triage.yml), which
  asserts on the artifacts the action produces — an action that has never executed
  is a YAML file with good intentions. That workflow needs no secret, so it works
  on forks.
- **A test guards the couplings** between `action.yml` and the CLI: the JSON keys
  it reads, the comment marker its upsert depends on, and every flag it passes.
  An Action breaks in someone else's repository with nobody watching the log.

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
5. **The run store must outlive the CI job**, and the example workflow uses
   `actions/cache` to demonstrate that, which is the wrong answer in production.
   Caches are best-effort, scoped per branch, and evicted without warning — so
   history will silently vanish and flake rates will silently reset. A real
   deployment points `[store] path` at shared storage.
6. Not validated against a production test suite. Deploying it would warrant a
   shadow-mode period in which recommendations are logged and not acted on.
7. **Quarantine granularity is per-test**, which is the wrong unit for an
   order-dependency flake: that failure is a property of a *pair* of tests, and
   quarantining whichever one happened to fail treats a symptom.

## What I would do differently, and what is next

Three concrete things, in the order I would actually do them.

**1. Replace the synthetic corpus with real anonymized failures.** This is the
weakest link in the whole repository and no amount of engineering elsewhere
compensates for it. Every accuracy number above is computed against 49 examples I
wrote myself, and I also wrote the prompt — so some of the classifier's margin is
probably shared vocabulary rather than shared reasoning. The fix is not more
synthetic examples; it is a few hundred real traces from a real suite, labelled by
somebody who is not me. Until that exists, the honest reading of the results table
is "the guardrails behave as designed on realistic-looking input", not "91.8%
accurate".

**2. Cascade the classifier instead of gating it.** The eval says four classes are
matched outright by a keyword list, and the model loses on `INFRA_FLAKE` recall
(60% against 100%). The current prefilter asks the wrong question: it decides
*whether* to spend a call, when the useful question is *which* model should answer.
A cascade — deterministic rules first, escalate only for the classes where the LLM
measurably wins — would cut cost substantially and *improve* accuracy on the
classes where it currently regresses. I did not build it because doing it before
having the measurement would have been guessing, and the measurement is what says
which classes to route.

**3. Add prompt caching, then re-measure.** The classify call spends ~2500 input
tokens against ~360 output, and most of the input is the stable taxonomy block sent
on every call. That is the single largest cost lever and it changes nothing about
behaviour. It is unbuilt for the same reason as (2): I would rather report
$0.0140 per test measured than a projected saving.

Two things I would change about how I built it rather than about what it does.
I would write the demo corpus **before** the detector rather than after — running
it is what exposed the flake-rate denominator bug, and the unit tests were happily
confirming the wrong definition until then. And I would have called the API on day
one instead of at phase P4: five of the constraints that shaped the client
(`temperature` rejected, the schema dialect subset, thinking blocks before text)
are things no amount of careful design would have predicted, and two of them would
have changed earlier decisions.

## Configuration

Thresholds live in [flaketriage.toml](flaketriage.toml) — flake-rate threshold,
window size, quarantine TTL, budget ceiling, confidence floor. There are no
behavioural constants in the source. The Anthropic API key is read from the
environment only; there is deliberately no config field for it, so it cannot be
committed by accident. See [.env.example](.env.example).

## License

MIT.
