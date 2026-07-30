# ADR-0003: Abstention over guessing

Status: accepted (phase P4)

## Context

A classifier that always answers looks better than one that sometimes declines.
Coverage is easy to read and easy to celebrate; abstentions look like failure. That
intuition is wrong for this tool, for a reason specific to how its output is used.

The output lands in a table an engineer reads while deciding whether to
investigate a red build. In that table, a wrong `RACE_CONDITION` and a right
`RACE_CONDITION` are typographically identical. A confident wrong label does not
degrade the tool's usefulness proportionally to its error rate — it inverts the
tool's purpose, because the engineer stops looking. `UNKNOWN` costs a reader a
few seconds. A plausible wrong cause costs them the investigation they would
otherwise have done.

## Decision

`UNKNOWN` is a first-class member of the taxonomy, and four separate mechanisms
route to it.

**1. The prompt says so explicitly.** Rule one, before anything about the
taxonomy: "UNKNOWN is a correct and expected answer... An honest UNKNOWN is more
useful than a plausible guess, because a guess will be read as a finding."

**2. Evidence is mandatory for any non-`UNKNOWN` cause.** A model that names a
cause but cites nothing has pattern-matched on the shape of the input. Empty or
whitespace-only evidence downgrades to `UNKNOWN` with reason `no_evidence`. This
is the cheapest hallucination guardrail available and it costs one `if`.

**3. A configurable confidence floor.** Below `classify.confidence_floor`
(default 0.55) a classification is downgraded rather than reported, because the
number does not survive contact with a table — nobody reads the 0.4.

**4. Every failure mode ends in `UNKNOWN`, not an exception.** Malformed JSON, an
invented cause code, a confidence out of range, an over-long field, an API
outage, an exhausted budget, a prefilter rejection. Each carries a distinct
`downgrade_reason`, so an abstention is always attributable to a mechanism rather
than being an unexplained blank.

Schema failures get **one repair attempt** with the reason fed back, because a
missing key is a different situation from a considered "nothing to cite". A second
failure abstains and logs the malformed text: two bad responses to one context is
a prompt or schema problem for a human, not something to retry into.

## Consequences

- **Abstention rate is a reported metric, measured separately from accuracy.**
  Accuracy conditional on not abstaining is reported alongside it. Reporting only
  one of the two would let either be gamed by moving the floor.
- **The floor is a published knob, not a hidden constant.** Someone who wants
  more coverage and less precision can have it, visibly, in a tracked file.
- **`REAL_REGRESSION` and `INFRA_FLAKE` are not flake categories**, so neither is
  "actionable" for policy. A regression is a defect and infra is not the test's
  fault; both reaching the quarantine path would be a serious error.
- **Some genuinely classifiable failures are refused.** The cost of guardrails
  that fire on thin evidence is that they occasionally fire on adequate evidence.
  The eval harness measures this as the abstention rate rather than hiding it.

## What this cost in practice

The first prefilter prompt asked whether there was "enough evidence to identify a
cause at all". A Haiku call answered *no* to a `ConnectionResetError` with a full
stack trace, reasoning that a generic network error did not pin down a cause. That
is a defensible answer to the question asked, and the wrong answer for a cost gate
— the abstention was silent and would have shown up in no metric except a lower
coverage number nobody would have investigated.

Two changes followed: the gate now asks a mechanical question about the *presence*
of evidence rather than an evaluative one about certainty, and every ambiguity in
reading its answer resolves toward spending the call. The asymmetry is stated in
the prompt and in the code: a wrong rejection loses a classification invisibly,
while a wrong escalation costs one call.

The lesson generalizes beyond the prefilter. An abstention mechanism that is
invisible in the metrics is indistinguishable from a bug, which is why every
abstention here carries a machine-readable reason.
