# Demo: one full pipeline run

A real transcript, not a mock-up. Twelve CI runs of a four-test suite are
ingested, then detected, triaged, and passed to policy. The scenario is
constructed so that each of the four interesting behaviours actually fires:

| test | what it is | what should happen |
|---|---|---|
| `test_concurrent_checkout` | a genuine race | flaky |
| `test_payment_capture` | an external dependency, plus one runner disk-full error | flaky, with the infra failure excluded |
| `test_totals_include_tax` | a real defect introduced at one commit | **regression**, never quarantined |
| `test_ships_to_eu` | stable | healthy, hidden from the report |

Log lines are JSON on stderr; the tables are on stdout. `httpx` request lines are
removed for readability and nothing else is edited.

## 1. Ingest

```
$ flaketriage ingest --results reports/run11-a1.xml --sha c02e7bb1a94d \
    --run-id run-11 --attempt 1 --branch main --diff change.patch

{"run_pk": 12, "run_id": "run-11", "attempt": 1, "commit_sha": "c02e7bb1a94d",
 "files_parsed": 1, "files_rejected": 0, "cases_ingested": 0,
 "cases_skipped_duplicate": 4, "new_identities": 0, "aliases_recorded": 0,
 "diff_files": 1, "warnings": 0, "event": "ingest_complete", "level": "info"}

 Ingested c02e7bb1a94d (run run-11, attempt 1)
 metric               value
 result files             1
 executions recorded      0
 duplicates skipped       4
 new test identities      0
 renames merged           0
 diff files               1
 parse warnings           0
```

This run was already ingested, so all four cases are **deduplicated rather than
double-counted**. That is the property that keeps a retried CI step from
corrupting every flake rate downstream, and it is visible in the output rather
than silent.

## 2. Detect — no model involved

```
$ flaketriage detect

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

Three things in that output are the point of the whole project.

**The regressing test is called a regression**, at high confidence, even though it
started failing partway through the window — which is exactly the shape that the
historical-instability signal would otherwise read as a flake.

**`test_payment_capture` shows 11 observations where the others show 12.** Its
disk-full error was excluded from the flake rate entirely, because attributing a
runner failure to a test author poisons the metric.

**Every row carries a confidence level.** "Diverged at one commit yesterday" and
"the flip rate crept over five percent" are both *flaky* and are not the same
claim.

`test_ships_to_eu` is healthy and therefore absent. A report that lists everything
is a report nobody reads.

## 3. Triage — the model proposes a cause

```
$ flaketriage triage --sha c02e7bb1a94d

{"kind": "prefilter", "model": "claude-haiku-4-5-20251001", "input_tokens": 505,
 "output_tokens": 8, "cost_usd": 0.000545, "latency_ms": 1562.3, "cache_hit": false,
 "prompt_version": "2026-07-30.1+908caee72aa0", "event": "llm_call"}

{"model": "claude-sonnet-5", "event": "model_rejects_temperature", "level": "info"}

{"kind": "classify", "model": "claude-sonnet-5", "input_tokens": 2481,
 "output_tokens": 349, "cost_usd": 0.012678, "latency_ms": 5290.5,
 "cache_hit": false, "prompt_version": "2026-07-30.1+908caee72aa0", "event": "llm_call"}

classified 1 test(s): 2 API call(s), 0 cache hit(s), 0 prefiltered, $0.0132

verdict     conf    rate  obs  cause (proposed)         test
regression  high      0%   12  -                        ...::test_totals_include_tax
flaky       medium   30%   11  EXTERNAL_DEPENDENCY 75%  ...::test_payment_capture

tests/integration/test_checkout.py::test_payment_capture
  - same_sha_divergence: 1 commit(s) produced both a pass and a failure; most recent: b91f4a2d7e0c
  - cross_attempt_divergence: outcome differed between attempts at b91f4a2d7e0c (attempts 1, 2)
  - historical_instability: flake rate 30.0% over 11 observations exceeds 5.0%,
    measured from same-commit divergence
  - proposed cause: EXTERNAL_DEPENDENCY (model confidence 75%, advisory only)
    The failure is a ConnectionResetError during an HTTP POST to PAYMENTS_URL,
    indicating a network-level failure to an external/third-party service rather
    than a code assertion failure. The same-commit divergence (pass and fail at the
    same SHA) and 30% flake rate confirm this is intermittent and
    environment-related, consistent with an unreliable external dependency.
    evidence: ConnectionResetError: [Errno 104] Connection reset by peer
    evidence: resp = session.post(PAYMENTS_URL, json=payload)
    evidence: same_sha_divergence: 1 commit(s) produced both a pass and a failure
    evidence: flake rate: 30.0% over 11 observations across 10 commit(s)
    suggested: Add retry/backoff logic or a mocked payment service in the test
    environment, and verify the external payments endpoint's stability.
```

The classification is right, and every item under `evidence:` is a string that was
actually in the input — no invented file names or log lines.

Note the third log line: **`model_rejects_temperature`**. `claude-sonnet-5` returns
a 400 for `temperature`, so the client dropped it and retried, and remembered the
model for the rest of the process. It is logged rather than swallowed, because it
means the spec's "temperature 0 for reproducibility" is not actually available here
— attributability comes from the recorded model id and `prompt_version` instead.
[ADR-0005](adr/0005-two-tier-model-cost-strategy.md).

Note also what the regression row does **not** have: a proposed cause. A regression
is not sent to the classifier at all. There is nothing for a model to add to "this
is a real defect and here is the commit", and asking would only create an
opportunity to disagree with a conclusion that rests on counting.

## 4. Policy — the deterministic decision

```
$ flaketriage policy --apply

                          Quarantine recommendations
 test                                                       rate  obs  owner       expires
 ...::test_payment_capture                                   30%   11  unresolved  2026-08-13
 ...::test_concurrent_checkout                               21%   12  unresolved  2026-08-13

Not recommended:
  tests/integration/test_checkout.py::test_totals_include_tax: cause_is_regression

                                  Open quarantines
 test                            state        cause                owner       expires
 ...::test_payment_capture       recommended  EXTERNAL_DEPENDENCY  unresolved  2026-08-13
 ...::test_concurrent_checkout   recommended  UNKNOWN              unresolved  2026-08-13
```

**The regression is refused, and the refusal says why.** `cause_is_regression` is a
named reason rather than an absence, because "why not this one?" is a question the
output should already answer.

Both recommendations carry a TTL — `2026-08-13`, fourteen days out — and an owner
field that says `unresolved` rather than inventing a name. This repository has no
`CODEOWNERS`, and the demo ran with the git fallback disabled, so the honest answer
is that nobody is assigned. A quarantine recommendation addressed to nobody is one
nobody acts on, and that is worth showing.

The second row's cause is `UNKNOWN` because that test was not classified in this
invocation — the `--sha` filter scoped triage to the latest commit. The quarantine
still stands: the deterministic evidence is sufficient on its own, which is the
whole point of the layering. [ADR-0004](adr/0004-quarantine-ttl.md).

## 5. Stats

```
$ flaketriage stats

    flaketriage metrics, last 30d
 metric                        value
 runs ingested                    12
 classifications                   1
 abstentions                0 (0.0%)
 cache hit rate                 0.0%
 API calls                         2
 failed API calls                  0
 total cost                  $0.0132
 cost per classification     $0.0132
 tokens in / out          2986 / 357
 classify latency P50        5290 ms
 classify latency P95        5290 ms

classifications by cause
 cause                n
 EXTERNAL_DEPENDENCY  1
```

Running `triage` again on the same evidence is a 100% cache hit at $0.00. The
tokens line shows where the cost actually is: 2986 in against 357 out, and most of
the input is the stable taxonomy block sent on every call — which is why prompt
caching is the first item in the README's "what next".

## Reproducing this

The scenario is synthetic JUnit XML, generated by the snippet in this repository's
history rather than committed as fixtures, because the point of the transcript is
the output rather than the input. To get the same shape:

```bash
uv run flaketriage ingest --results ./reports --sha "$SHA" --run-id "$RUN" --attempt 1
uv run flaketriage detect
uv run flaketriage triage --no-llm      # or drop --no-llm with a key set
uv run flaketriage policy
uv run flaketriage stats
```

Everything except step 3 without `--no-llm` runs with zero API calls.
