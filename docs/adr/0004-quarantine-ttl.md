# ADR-0004: Quarantine carries a TTL, an owner, and an exit condition

Status: accepted (phase P7)

## Context

Quarantine is the only thing this tool recommends that changes anything, and it
is the point at which a flake-triage system usually starts doing harm.

The failure mode is well documented and always the same shape. A test is flaky, so
it is quarantined to unblock the pipeline. The quarantine has no expiry, nobody
owns it, and there is no defined route back. Two years later the suite has three
hundred quarantined tests, nobody knows which of them still describe real
behaviour, and the ones that were catching genuine bugs have been silently not
catching them the whole time. The quarantine list has become a deletion list that
nobody had to argue for.

Everything in this ADR exists to make that outcome structurally awkward.

## Decision

**A recommendation is a conjunction of four conditions**, all of which must hold:
flake rate above threshold, minimum observation count met, cause not
`REAL_REGRESSION` and not `INFRA_FLAKE`, and no quarantine already open for the
test. Any single failure is reported as a named `RefusalReason` rather than as a
silent absence — "why not this one?" is a question the output should already
answer.

**The regression check consults both layers.** The detector's verdict and the
model's cause are checked independently, and either one saying "regression" is
sufficient to refuse. They can disagree: the detector may lack the history to see
a transition that the diff makes obvious, and the model may misread an
intermittent failure. Requiring agreement would let a defect through whenever one
layer was wrong; requiring only one is the conservative direction.

**The model can veto but never cause.** The classification is one field in the
conjunction, and the flake rate and observation count must be met independently. A
model that hallucinated `RACE_CONDITION` on every test in the suite could not
quarantine anything the deterministic evidence had not already condemned. That is
ADR-0001's boundary made concrete, and there is a test asserting it.

**Every recommendation carries a TTL** (default 14 days). Expiry is applied when
the store is read rather than by a background job — there is no daemon, so a TTL
that only advanced while something was running would never advance at all.

**Expired and released are different terminal states.** *Released* means the test
earned its way back: the configured number of consecutive clean executions
accumulated and the quarantine closed automatically. *Expired* means the TTL ran
out with the test still unstable, and a human has to decide. Collapsing both into
"closed" would hide the only distinction a reader cares about, because the second
one is a request for a decision and the first is not.

**Every recommendation carries an owner, and the owner's provenance.** Resolution
prefers `CODEOWNERS` and falls back to the last committer to the test's file, and
the two are labelled differently in output. A name from `CODEOWNERS` is a
statement of responsibility; the last committer is a guess that happens to be
usually right — they may have fixed a typo in a test somebody else owns.
Presenting them identically would let a guess inherit the authority of a
declaration. When neither resolves, the recommendation says "unresolved" rather
than inventing an owner.

**A quarantined test keeps running.** It stops *blocking*; it does not stop
executing. This is the design decision reviewers probe, and the reasoning is
mechanical rather than philosophical: the exit condition is N consecutive clean
executions, so a test that no longer executes can never satisfy it. A quarantine
that removes its own exit condition is a permanent deletion with extra steps.
Nothing in the schema records a test as disabled, because the tool never disables
one.

**The tool never changes CI state.** `flaketriage policy` is read-only by
default; `--apply` writes to this tool's own records and nothing else. It does not
edit test files, does not add skip markers, and does not touch workflow
configuration. Acting on a recommendation stays a human action, per §13.

## Consequences

- **Recommendations accumulate if nobody acts on them.** Deliberate. The
  alternative is a tool that silently disables tests, which is the thing this ADR
  exists to prevent.
- **Automatic release needs enough clean executions to be meaningful**, and the
  default (20) is a guess calibrated on nothing. It is in `flaketriage.toml` and
  should be tuned against a real suite's run frequency.
- **The `CODEOWNERS` parser is a documented subset.** Literal paths, directory
  prefixes and `*`/`**` globs; character classes and negation are unsupported and
  simply do not match, so the fallback runs and the owner is labelled as a guess.
  Silently mis-resolving an owner is worse than declining to resolve one.
- **Skips do not count toward release.** A test that did not run has not earned
  its way out of quarantine, so skipped executions are excluded from the clean-run
  count rather than treated as clean.
- **A per-test quarantine is the wrong granularity for some flakes.** An
  order-dependency failure is a property of a *pair* of tests, and quarantining
  the one that happens to fail is treating a symptom. Noted as future work; the
  taxonomy at least names the case.

## Alternatives rejected

- **Quarantine with no TTL.** The status quo this ADR is a reaction to.
- **Manual release only.** A quarantine that only a human can lift is a
  quarantine nobody lifts; the whole problem is that nobody revisits the list.
- **Auto-quarantine without human confirmation.** Explicitly out of scope (§13),
  and it would make the model's classification load-bearing on a decision that
  suppresses signal.
- **Letting the LLM make the call.** The reason this project exists is to
  demonstrate that it should not. See ADR-0001.
