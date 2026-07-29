# ADR-0002: Test identity is a normalized triple plus explicit, labelled aliasing

Status: accepted (phase P1–P2)

## Context

Every number this tool produces is computed over a test's execution history, so
history is only as good as the key it hangs on. The obvious key — the
`classname#method` string the reporter emits — breaks in four ordinary
situations, each of which silently resets a test's history to zero:

1. **Parameterized tests.** `test_login[user=alice]` and `test_login[user=bob]`
   are separate cases sharing a logical parent. Collapse them and per-case flake
   rates are gone; keep them fully distinct and a 200-case parameter matrix is
   200 tests with no history each.
2. **Dialect disagreement.** pytest emits a dotted module path, Surefire a Java
   FQCN, Playwright a nested describe path, jest-junit whatever it was
   configured to emit. The same physical test yields different strings depending
   on who wrote the reporter.
3. **Renames.** A test renamed during a refactor looks like one test vanishing
   and another appearing.
4. **File moves.** Moving a spec into a subdirectory changes the path component
   without touching the test at all.

The damaging property is shared: history resets exactly when an engineer is
touching a flaky test, which is when its history is most needed.

## Decision

**Primary key: a normalized `(suite_path, test_name, parameters)` triple,
hashed.** Parameterization is split out of the name, so instances stay distinct
while sharing a base name. Suite path is derived most-specific-first: a real
file path if the reporter gave one, else a dotted classname converted to a path,
else the nested `<testsuite>` names. A Java class stays distinct from its
package (`com/example::OrderTest`) so two classes in one package do not
collapse.

The key is a truncated SHA-256 of the triple with `\x1f` separators rather than
the concatenated string: fixed width, safe to index, and a change to the
normalization rules yields a visibly different key instead of a silently
reinterpreted one.

**Renames and moves: an explicit alias table, populated by inference.** When an
identity produces no execution in a run and a previously unseen identity appears
in the same run, they are candidates for the same logical test. A candidate is
accepted when all three hold:

- Parameters match exactly. Two parameter instances are never renames of each
  other.
- The combined distance — half the normalized name distance plus half the
  normalized suite-path distance — is within `identity.alias_max_distance`
  (default 0.25). The even split lets a pure rename and a pure move each clear
  the bar, while a test renamed *and* moved at once does not.
- The pairing is unambiguous. If two disappeared tests compete for one appeared
  test at the same distance, neither is merged and both endpoints are removed
  from matching entirely.

**Uncertainty is labelled, not hidden.** Only distances within
`identity.alias_certain_distance` (default 0.03 — typo-level) are recorded as
certain. Everything else is stored with `certain = 0` and surfaced downstream as
`merged_uncertain`, including in the CLI output of `ingest`.

## Consequences

Accepted costs, stated rather than buried:

- **Aliasing is heuristic and can merge incorrectly.** Two tests with similar
  names in the same file, where one is deleted in the same commit that adds the
  other, will be merged. The `merged_uncertain` label is the mitigation; it is a
  warning, not a fix.
- **A test renamed and moved in one commit loses its history.** This is
  deliberate. Both signals changed, so there is no evidence to distinguish that
  test from an unrelated deletion plus addition, and inventing a merge would
  produce a flake rate describing no real test.
- **Reporter changes can orphan history.** A pipeline that starts emitting
  `file` attributes changes the suite path for every test at once. Aliasing
  handles this only where the derived paths stay similar.
- **Parameterization conventions beyond `[...]` are not recognized.** Reporters
  that flatten parameters into the name with a separator (`"adds 1 + 2"`) are
  indistinguishable from ordinary names without a per-framework rule. No guess is
  made, because guessing would merge genuinely distinct tests.

## Alternatives rejected

- **Content hashing the test body.** Correctly handles renames and moves, but
  breaks on every edit to the test — including the edit that fixes the flake, so
  a fixed test would still be reported as unstable.
- **Reading git rename detection.** Stronger evidence than name similarity for
  file moves, and worth adding as a corroborating signal that promotes a merge to
  certain. It does not cover renames within a file, so it complements this
  approach rather than replacing it. Listed as future work.
- **Never merging.** Simple and never wrong, but makes flake rate useless in any
  codebase under active refactoring.

## Verification

`tests/test_identity_reconcile.py` includes two property-based tests: any
single-character substitution in a name of twelve characters or more preserves
history across the rename, and any pair whose distance exceeds the configured
ceiling is never merged. The second is the one that matters — a merge rule
without a false-merge guard is worse than no merge rule.
