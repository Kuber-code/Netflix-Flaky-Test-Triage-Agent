"""Prompt construction and context assembly.

Context is a cost *and* an accuracy problem, in the same direction: a trace
padded out with forty frames of framework internals costs more and classifies
worse, because the signal is diluted by noise the model has to wade through. So
the assembled context is deliberately tight:

* The failure message and the head of the trace, with project frames kept and
  framework frames dropped.
* Only the diff hunks touching files that appear in the trace. A full diff is
  mostly irrelevant to any one test and invites the model to invent a connection.
* A compact history summary -- recent outcomes, flake rate, whether failures are
  confined to one shard -- rather than raw execution rows.
* Which detector signals fired, because "diverged at the same commit" and "the
  flip rate crept up" call for different readings of the same trace.

``PROMPT_VERSION`` is hashed into every output record. A prompt change that moves
the numbers must be attributable to that change rather than to drift.
"""

from __future__ import annotations

import hashlib
from typing import Any, Final

from flaketriage.classify.taxonomy import CAUSE_GUIDANCE, CauseCode
from flaketriage.detect.footprint import extract_paths, is_noise_line
from flaketriage.detect.models import Detection
from flaketriage.models import DiffSummary

PROMPT_VERSION: Final = "2026-07-30.1"

_HEAD_FRACTION: Final = 0.6

SYSTEM_PROMPT: Final = """\
You classify the likely cause of a failing or flaky software test into a fixed \
taxonomy. You are one input to a triage tool; a deterministic policy engine makes \
every decision that changes CI state. Your job is to read the evidence \
accurately, not to be helpful about it.

Rules, in order of importance:

1. UNKNOWN is a correct and expected answer. Use it whenever the evidence \
supports two causes equally well, or supports none specifically. An honest \
UNKNOWN is more useful than a plausible guess, because a guess will be read as a \
finding.
2. Every non-UNKNOWN answer must cite concrete evidence: strings quoted or \
referenced from the input you were given. If you cannot point at anything, the \
answer is UNKNOWN. Do not invent file names, symbols, or log lines.
3. Report REAL_REGRESSION when the evidence indicates a genuine defect, even \
though the test was flagged as a flake candidate. A real bug dismissed as noise \
is the most expensive error this tool can make.
4. Report INFRA_FLAKE only when the failure is at the harness or scheduler level \
with no test assertion involved. Blaming a test author for a platform outage \
destroys trust in the tool.
5. Distinguish an assertion failure from a harness error. An assertion failure \
means the framework ran the test and the check did not hold, which points at the \
code under test. A harness error points at the environment.

Cause codes and the evidence that supports each:

{taxonomy}

Set confidence to how well the evidence supports the cause: above 0.8 only when \
the trace or history names the mechanism directly, 0.5-0.8 when the evidence is \
consistent with the cause but also with others, below 0.5 when you are guessing \
-- and if you are guessing, answer UNKNOWN instead."""

REPAIR_INSTRUCTION: Final = """\
Your previous response could not be parsed against the required schema. Reason: \
{reason}.

Return only a JSON object with exactly these keys: cause (one of the listed \
codes), confidence (number 0.0-1.0), reasoning (string), evidence (array of \
strings), suggested_action (string), abstained (boolean). No prose, no code \
fence. If you are unsure, return cause UNKNOWN with abstained true."""

# Framed as a mechanical check for the *presence* of evidence, not as a judgement
# about whether a cause can be determined. The first version asked the latter and
# a Haiku call answered "no" to a ConnectionResetError with a full stack trace,
# reasoning that a "generic network error" did not pin down a cause -- which is a
# fair answer to the question it was asked and the wrong answer for a cost gate.
# The gate's only job is to skip failures with nothing in them.
PREFILTER_SYSTEM_PROMPT: Final = """\
You are a cost gate in front of a more expensive classifier. Your only job is to \
skip failures that contain no usable detail at all. You are NOT deciding what \
caused the failure, and you are NOT judging whether the cause is obvious.

Set classifiable = true if ANY of these is present:
- a named exception or error type (for example ConnectionResetError, \
AssertionError, TimeoutError)
- a failure message with any specific content, even a generic-sounding one
- a stack trace with any file or line reference
- a note that failures are confined to particular shards

Set classifiable = false ONLY when the input has none of the above: an empty or \
missing message, a bare "test failed", or a trace that was truncated away.

A named exception type always counts as usable detail, however ordinary it looks. \
When in doubt, set true: a wrong false silently loses a classification, while a \
wrong true costs one call."""

#: Schema for the gate. A boolean field rather than prose because the model
#: reliably ignores "reply with exactly YES or NO" and adds an explanation, which
#: both costs tokens and turns the parse into a guess about prefixes.
PREFILTER_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "classifiable": {
            "type": "boolean",
            "description": "True if the input contains any usable failure detail.",
        }
    },
    "required": ["classifiable"],
    "additionalProperties": False,
}


def system_prompt() -> str:
    taxonomy = "\n".join(f"- {code.value}: {CAUSE_GUIDANCE[code]}" for code in CauseCode)
    return SYSTEM_PROMPT.format(taxonomy=taxonomy)


def prompt_version_hash() -> str:
    """Hash of every prompt that can change the outcome, recorded in each output.

    Covers the taxonomy guidance as well as the instructions, because changing
    what evidence a code is described by changes behaviour as much as changing the
    rules -- and covers the **prefilter** prompt too, which is less obvious and was
    a real bug: the gate decides whether a classification happens at all, so a
    cached ``PREFILTERED`` abstention has to be invalidated when the gate's
    wording changes. Leaving it out meant a fixed prefilter prompt kept serving the
    rejections the broken one had produced.
    """
    payload = (
        PROMPT_VERSION + system_prompt() + REPAIR_INSTRUCTION + PREFILTER_SYSTEM_PROMPT
    ).encode()
    return f"{PROMPT_VERSION}+{hashlib.sha256(payload).hexdigest()[:12]}"


def truncate_trace(trace: str | None, budget: int) -> str:
    """Keep the head and the project frames; drop framework noise.

    Head-and-project rather than head-only: the exception and its immediate
    context are at the top, but the frame that names the project's own code is
    often further down, past a wall of framework internals.
    """
    if not trace:
        return ""
    text = trace.strip()
    if len(text) <= budget:
        return text

    lines = text.splitlines()
    head_budget = int(budget * _HEAD_FRACTION)

    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1

    remaining = budget - used
    tail: list[str] = []
    for line in lines[len(head) :]:
        if is_noise_line(line) or not _looks_like_project_line(line):
            continue
        if remaining - len(line) - 1 < 0:
            break
        tail.append(line)
        remaining -= len(line) + 1

    parts = head
    if tail:
        parts = [*head, "    ... framework frames omitted ...", *tail]
    return "\n".join(parts)


def _looks_like_project_line(line: str) -> bool:
    return bool(extract_paths(line))


def relevant_diff(diff: DiffSummary | None, footprint: tuple[str, ...]) -> str:
    """The slice of the diff touching files the failure actually mentions.

    A whole diff would let the model connect the failure to any change at all,
    which is exactly the kind of confident, unfalsifiable reasoning that makes an
    LLM triage tool untrustworthy.
    """
    if diff is None or not footprint:
        return ""

    lines: list[str] = []
    for path in footprint:
        change = diff.change_for(path)
        if change is None:
            continue
        ranges = ", ".join(
            f"{line_range.start}-{line_range.end}" for line_range in change.new_ranges
        )
        entry = f"- {change.path} ({change.change_type.value})"
        if ranges:
            entry += f" changed lines {ranges}"
        if entry not in lines:
            lines.append(entry)

    if not lines:
        return "The change under test touches none of the files this test's failure mentions."
    return "\n".join(lines)


def history_summary(detection: Detection) -> str:
    """Compact history: the facts the detector established, not raw rows."""
    parts = [
        f"- verdict: {detection.verdict.value} (detector confidence: {detection.confidence.value})",
        f"- flake rate: {detection.flake_rate:.1%} over {detection.observations} "
        f"observations across {detection.windows} commit(s)",
    ]
    if detection.retry_data_available:
        parts.append(
            f"- same-commit divergence rate: {detection.divergence_rate:.1%} "
            "(this pipeline retries, so divergence is directly observable)"
        )
    else:
        parts.append(
            "- this pipeline does not retry, so same-commit divergence cannot be "
            f"observed; cross-commit flip rate is {detection.intermittency_rate:.1%}"
        )
    if detection.failing_shards:
        parts.append(
            f"- failures are confined to shard(s) {', '.join(detection.failing_shards)}, "
            "which can indicate order dependency"
        )
    if detection.infra_excluded:
        parts.append(
            f"- {detection.infra_excluded} execution(s) were excluded as "
            "platform failures and are not in the rate above"
        )
    if detection.merged_uncertain:
        parts.append(
            "- this history was merged across an inferred rename and may combine "
            "two tests; treat the history as less reliable than the trace"
        )
    return "\n".join(parts)


def signals_summary(detection: Detection) -> str:
    if not detection.signals:
        return "- none fired; this test was selected on its failure alone"
    return "\n".join(
        f"- {evidence.signal.value}: {evidence.detail}" for evidence in detection.signals
    )


def build_context(
    detection: Detection,
    *,
    diff: DiffSummary | None = None,
    trace_budget_chars: int = 2000,
    test_source: str | None = None,
) -> str:
    """Assemble the user message for one test.

    The text is also the cache key (see :mod:`flaketriage.classify.cache`), so it
    must contain everything that could change the answer and nothing that varies
    run to run for the same evidence -- no timestamps, no run ids.
    """
    sections = [
        f"## Test\n{detection.identity.display_name}",
        f"## Outcome\n{_outcome_line(detection)}",
    ]

    failure_detail = _failure_block(detection, trace_budget_chars)
    sections.append(f"## Failure detail\n{failure_detail}")

    if test_source:
        sections.append(f"## Test source\n```\n{test_source.strip()}\n```")

    sections.append(f"## Execution history\n{history_summary(detection)}")
    sections.append(f"## Detector signals\n{signals_summary(detection)}")

    diff_text = relevant_diff(diff, detection.footprint)
    if diff_text:
        sections.append(f"## Change under test (only files this failure mentions)\n{diff_text}")

    sections.append(
        "## Task\nClassify the most likely cause. Cite evidence from the sections "
        "above. Answer UNKNOWN if the evidence does not distinguish one cause."
    )
    return "\n\n".join(sections)


def _outcome_line(detection: Detection) -> str:
    outcome = detection.latest_outcome.value if detection.latest_outcome else "unknown"
    kind = (
        "assertion failure (the framework ran the test and the check did not hold)"
        if outcome == "fail"
        else "harness error (the test did not complete normally)"
        if outcome == "error"
        else outcome
    )
    return f"Most recent outcome: {kind}"


def _failure_block(detection: Detection, trace_budget_chars: int) -> str:
    lines = []
    if detection.failure_type:
        lines.append(f"Type: {detection.failure_type}")
    if detection.failure_message:
        lines.append(f"Message: {detection.failure_message}")
    trace = truncate_trace(detection.stack_trace, trace_budget_chars)
    if trace:
        lines.append(f"Trace:\n```\n{trace}\n```")
    if not lines:
        # Said explicitly rather than left blank: "no detail" is itself the
        # evidence that should push the model toward UNKNOWN.
        return "No failure message or trace was captured for this failure."
    return "\n".join(lines)


def build_prefilter_context(detection: Detection, *, trace_budget_chars: int = 600) -> str:
    """A much smaller context for the cheap triage gate."""
    return "\n".join(
        [
            f"Test: {detection.identity.display_name}",
            f"Outcome: {detection.latest_outcome.value if detection.latest_outcome else '?'}",
            _failure_block(detection, trace_budget_chars),
            f"Signals: {', '.join(detection.signal_codes) or 'none'}",
        ]
    )
