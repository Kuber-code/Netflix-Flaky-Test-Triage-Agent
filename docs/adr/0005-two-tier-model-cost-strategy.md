# ADR-0005: Two-tier model strategy and cost controls

Status: accepted (phase P4; measured in P5)

## Context

A CI-triggered tool runs on every red build, unattended. Cost is not a rounding
error at that frequency, and an unbounded per-invocation spend is the kind of
thing that gets a tool switched off after one bad week.

Measured on a two-test corpus with a cold cache, against the configured models:

| call | model | input | output | cost | latency |
|---|---|---|---|---|---|
| prefilter | `claude-haiku-4-5` | 539 | 8 | $0.00058 | 1.1s |
| classify | `claude-sonnet-5` | 2498 | 362 | $0.01292 | 4.2s |

So roughly **$0.0134 per classified test**, cold. The input side of the classify
call dominates, and most of it is the system prompt carrying the full taxonomy —
which means context assembly is the cost lever, not output length.

## Decision

Four controls, all measured rather than asserted.

**1. Content-addressed cache.** Keyed on a hash of the assembled context, the
model identity, and the prompt version hash. The same trace recurs across runs —
that is what makes a flake a flake — so the repeat is free. Measured hit rate on a
second identical invocation: 100%, $0.00.

All three key components are load-bearing. Omitting the prompt version means a
prompt edit reads yesterday's answers; omitting the model means two models share
entries. The gate model is included too, because the gate can turn a classifiable
failure into an abstention, so two runs with different gates are not
interchangeable even when the expensive model matches.

**2. Cheap-model prefilter.** Haiku answers one mechanical question — does this
input contain any usable failure detail? — for about 4% of a classify call.
Rejections are recorded as `PREFILTERED` abstentions, not dropped, so the harness
can measure what the gate costs in accuracy rather than only what it saves.

**3. Hard budget cap.** Checked before each call, not after. The guarantee is
stated precisely because it cannot be absolute: spend is bounded by the ceiling
plus at most one call, since a call's cost is unknown until it returns. After the
first call the observed mean is used as the estimate, so the cap is effectively
exact from then on. Remaining tests are emitted as `UNKNOWN` with reason
`budget_exhausted` — never silently omitted, because a missing row reads as "no
problem here".

**4. Batch bounding.** `classify.max_tests` caps tests per invocation, applied
after sorting by flake rate, so a limited budget goes to the tests most worth
explaining. The overflow is marked, not dropped.

**Determinism.** Temperature 0 is requested, and the model identifier plus the
prompt version hash are recorded on every output record so a result is always
attributable.

## Consequences and constraints found by measuring

- **`claude-sonnet-5` rejects `temperature` outright** — a 400 saying it is
  deprecated. The client sends it, and on that specific rejection drops it and
  retries, remembering the model for the process. So the spec's "temperature 0 for
  reproducibility" is *not* fully available on the configured classifier;
  attributability comes from the recorded model and prompt hash instead. Stating
  this beats claiming a determinism the API will not give.
- **The structured-output schema dialect is a subset of JSON Schema.** `minimum`
  and `maximum` on a number are rejected. This is the concrete reason the Pydantic
  layer is not redundant with the API's schema enforcement: the range is stated in
  the field description and enforced after the response arrives.
- **Prices live in `flaketriage.toml`, not in code.** Published prices change, and a
  number baked into source is a number nobody re-checks. An unpriced model costs
  0.00 and logs a warning rather than raising — a visibly wrong figure in a report
  beats losing the classifications already paid for.
- **The cache is a plain directory of JSON files.** A corrupt entry costs one
  re-classification, entries can be read and deleted by hand, and writes are
  atomic via a temp file and rename.
- **Transient abstentions are never cached.** An API error or an exhausted budget
  describes the run, not the evidence; caching them would make one bad afternoon
  permanent.

## Not done

- **Prompt caching for the system prompt.** The taxonomy block is ~2000 stable
  input tokens on every call and is the obvious next cost lever. Left out because
  it should be justified against measured savings, which is P5's job.
- **Multi-model ensembles or voting.** Explicitly out of scope (§13), and would
  multiply cost for a classification task over short contexts.
