# ADR-0001: Deterministic core, LLM advisory

Status: accepted (phase P0 onward)

## Context

There are three separable questions in flake triage, and they have different
epistemic status:

1. **Is this test flaky?** A question about observed outcomes.
2. **Why is it flaky?** A question about interpreting messy semi-structured text.
3. **What should we do about it?** A decision with asymmetric costs.

The default shape for an LLM-powered tool is to hand all three to the model:
feed it the results and the traces, ask for a verdict and a recommendation. That
shape is easy to build and impossible to trust. It has no floor — when the model
is wrong, unavailable, rate-limited, or newly retrained, the tool produces
nothing usable and there is no way to tell a good run from a bad one.

## Decision

Split the three questions by the kind of reasoning each requires.

**Question 1 is arithmetic, and is answered without a model.** If the same commit
SHA produced both a pass and a failure, the test is non-deterministic — not
"probably", not "the model thinks so". Detection is counting over per-commit
windows, plus an exponentially weighted flake rate and a separate regression
path. `flaketriage detect` and `flaketriage triage --no-llm` are complete,
useful, and make zero API calls.

**Question 2 is where the model earns its place.** Deciding that a trace showing
a thread-pool frame and an intermittent assertion on shared state is a race
condition, rather than an external dependency failure, is pattern-matching over
prose. A regex can do the obvious half — which is exactly why the evaluation
harness includes a keyword baseline the model has to beat, per class, with the
losses reported too.

**Question 3 stays deterministic, and the model's answer is one input.** The
quarantine decision has asymmetric costs: quarantining a test that is actually
detecting a real bug suppresses a real signal. The policy engine consults flake
rate, observation count, existing quarantine state and the classified cause, and
the LLM contributes only that last one. The LLM proposes; policy decides.

## Consequences

Enforced structurally, not by convention:

- **The import graph runs one way.** `detect`, `ingest`, `identity`, `store`,
  `policy` and `report` never import `classify`. A test walks the AST of every
  module in those packages and fails if that edge appears, because `--no-llm`
  stops being a guarantee the moment it does.
- **The flake rate is computed before any model runs.** Which means the
  `INFRA_FLAKE` exclusion cannot depend on the classifier: platform failures are
  identified deterministically from configured patterns, or the rate would only
  be correct when the LLM layer is enabled.
- **The detector emits confidence, never a bare boolean.** "Diverged twice at the
  same commit yesterday" and "the flip rate crept over five percent" are both
  "flaky" and are not the same claim.
- **Missing credentials are a mode, not an error.** No `ANTHROPIC_API_KEY` means
  the deterministic path runs. It does not mean the tool fails.

Costs accepted:

- **More code than a prompt wrapper.** The detector, the store, and the identity
  layer are most of the codebase, and none of it would exist in a
  pass-everything-to-the-model design.
- **The deterministic core needs retry data to be at its best.** Same-commit
  divergence requires a pipeline that retries. Without it, detection falls back
  to the weaker cross-commit intermittency measure and says so in the output. A
  model asked to guess from a single trace would appear to work in that case,
  which is worse than being visibly limited.
- **Two thresholds to tune instead of one prompt to edit.** Deliberate: a
  threshold in a tracked config file is reviewable and its effect is
  reproducible.

## Alternatives rejected

- **LLM decides everything.** No floor, no measurable accuracy, no defensible
  quarantine decision.
- **No LLM at all.** A pure regex classifier is implemented — as the evaluation
  baseline. It handles unambiguous signatures (`ConnectionError`, `timeout`) and
  degrades on anything requiring the trace to be read in context. The comparison
  is published rather than asserted, including the classes where the baseline
  wins.
- **LLM decides, deterministic rules veto.** Closer, but it inverts
  accountability: the interesting cases become the ones where the veto fires, and
  the veto rules end up re-deriving the detector anyway — with the LLM's answer
  already anchoring the report.

## Verification

`tests/test_detect_pipeline.py::test_the_deterministic_core_does_not_import_the_classifier`
walks the import graph. `tests/test_cli_detect.py::test_triage_no_llm_produces_a_report_with_zero_api_calls`
covers the user-facing guarantee. The eval harness (phase P5) publishes the
accuracy the LLM layer adds, and the baseline it must beat to justify its cost.
