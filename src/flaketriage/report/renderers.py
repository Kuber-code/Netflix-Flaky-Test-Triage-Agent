"""Report renderers: terminal, JSON, and markdown.

All three read the same :class:`Detection` list, so no renderer can show a
different verdict from another. The JSON form is the contract for downstream
consumers and includes the numbers behind each verdict, not just the label --
a consumer that disagrees with the thresholds can re-derive its own conclusion
without re-running detection.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final

from rich.console import Console
from rich.table import Table

from flaketriage.detect.models import Confidence, Detection, Verdict
from flaketriage.models import Classification

# Verdicts, in the order a reader should care about them. Regressions first:
# a real defect misfiled as noise is the expensive outcome.
_VERDICT_ORDER: Final = (
    Verdict.REGRESSION,
    Verdict.FLAKY,
    Verdict.PERSISTENT_FAILURE,
    Verdict.NEW_FAILURE,
    Verdict.HEALTHY,
)

_VERDICT_STYLE: Final = {
    Verdict.REGRESSION: "bold red",
    Verdict.FLAKY: "yellow",
    Verdict.PERSISTENT_FAILURE: "red",
    Verdict.NEW_FAILURE: "cyan",
    Verdict.HEALTHY: "green",
}

#: Hidden HTML marker identifying a comment this tool owns.
COMMENT_MARKER: Final = "<!-- flaketriage:report -->"

_CONFIDENCE_STYLE: Final = {
    Confidence.HIGH: "bold",
    Confidence.MEDIUM: "",
    Confidence.LOW: "dim",
}


def sort_for_report(detections: Sequence[Detection]) -> list[Detection]:
    """Most consequential first: by verdict class, then by flake rate."""
    order = {verdict: index for index, verdict in enumerate(_VERDICT_ORDER)}
    return sorted(
        detections,
        key=lambda detection: (
            order.get(detection.verdict, len(order)),
            -detection.flake_rate,
            detection.identity.display_name,
        ),
    )


def to_dict(detection: Detection, classification: Classification | None = None) -> dict[str, Any]:
    """One detection as a JSON-serializable record.

    The classification is nested under its own key rather than flattened in, so a
    consumer can never mistake a model's proposed cause for a deterministic
    finding. That separation is the whole design position -- see ADR-0001.
    """
    record: dict[str, Any] = {
        "test": detection.identity.display_name,
        "identity_id": detection.identity_id,
        "fingerprint": detection.identity.fingerprint,
        "suite_path": detection.identity.suite_path,
        "test_name": detection.identity.test_name,
        "parameters": detection.identity.parameters,
        "verdict": detection.verdict.value,
        "confidence": detection.confidence.value,
        "signals": [
            {
                "signal": evidence.signal.value,
                "detail": evidence.detail,
                "observations": evidence.observations,
            }
            for evidence in detection.signals
        ],
        "flake_rate": round(detection.flake_rate, 4),
        "divergence_rate": round(detection.divergence_rate, 4),
        "intermittency_rate": round(detection.intermittency_rate, 4),
        "retry_data_available": detection.retry_data_available,
        "observations": detection.observations,
        "commits_observed": detection.windows,
        "infra_excluded": detection.infra_excluded,
        "latest_sha": detection.latest_sha,
        "latest_outcome": detection.latest_outcome.value if detection.latest_outcome else None,
        "regression_sha": detection.regression_sha,
        "failure_type": detection.failure_type,
        "failure_message": detection.failure_message,
        "footprint": list(detection.footprint),
        "failing_shards": list(detection.failing_shards),
        "merged_uncertain": detection.merged_uncertain,
    }
    if classification is not None:
        record["classification"] = {
            "cause": classification.cause.value,
            "confidence": round(classification.confidence, 3),
            "reasoning": classification.reasoning,
            "evidence": list(classification.evidence),
            "suggested_action": classification.suggested_action,
            "abstained": classification.abstained,
            "downgrade_reason": classification.downgrade_reason.value,
            "model": classification.model,
            "prompt_version": classification.prompt_version,
        }
    return record


def render_json(
    detections: Sequence[Detection],
    *,
    llm_enabled: bool = False,
    classifications: dict[int, Classification] | None = None,
) -> str:
    resolved = classifications or {}
    payload = {
        "schema_version": 1,
        "llm_enabled": llm_enabled,
        "summary": summarize(detections),
        "detections": [
            to_dict(detection, resolved.get(detection.identity_id))
            for detection in sort_for_report(detections)
        ],
    }
    return json.dumps(payload, indent=2, sort_keys=False)


def summarize(detections: Sequence[Detection]) -> dict[str, int]:
    counts = {verdict.value: 0 for verdict in _VERDICT_ORDER}
    for detection in detections:
        counts[detection.verdict.value] += 1
    counts["total"] = len(detections)
    counts["merged_uncertain"] = sum(1 for d in detections if d.merged_uncertain)
    return counts


def render_terminal(
    detections: Sequence[Detection],
    console: Console,
    *,
    show_healthy: bool = False,
    classifications: dict[int, Classification] | None = None,
) -> None:
    """Print a table of findings. Healthy tests are hidden unless asked for."""
    rows = [
        detection
        for detection in sort_for_report(detections)
        if show_healthy or detection.verdict is not Verdict.HEALTHY
    ]

    if not rows:
        console.print(
            f"[green]No flakes, regressions or unexplained failures[/green] "
            f"across {len(detections)} test(s)."
        )
        return

    resolved = classifications or {}
    table = Table(box=None, pad_edge=False)
    table.add_column("verdict")
    table.add_column("conf")
    table.add_column("rate", justify="right")
    table.add_column("obs", justify="right")
    if resolved:
        table.add_column("cause (proposed)")
    table.add_column("test", overflow="fold")

    for detection in rows:
        style = _VERDICT_STYLE.get(detection.verdict, "")
        name = detection.identity.display_name
        if detection.merged_uncertain:
            name += " [dim](merged_uncertain)[/dim]"
        cells = [
            f"[{style}]{detection.verdict.value}[/{style}]" if style else detection.verdict.value,
            f"[{_CONFIDENCE_STYLE[detection.confidence]}]{detection.confidence.value}[/]"
            if _CONFIDENCE_STYLE[detection.confidence]
            else detection.confidence.value,
            f"{detection.flake_rate:.0%}" if detection.observations else "-",
            str(detection.observations),
        ]
        if resolved:
            cells.append(_cause_cell(resolved.get(detection.identity_id)))
        cells.append(name)
        table.add_row(*cells)

    console.print(table)
    console.print()

    for detection in rows:
        classification = resolved.get(detection.identity_id)
        if not detection.signals and classification is None:
            continue
        console.print(f"[bold]{detection.identity.display_name}[/bold]")
        for evidence in detection.signals:
            console.print(f"  - {evidence.signal.value}: {evidence.detail}")
        if detection.regression_sha:
            console.print(
                f"  - regression pivots on {detection.regression_sha[:12]}; "
                "not eligible for quarantine"
            )
        if classification is not None:
            _print_classification(console, classification)
        console.print()

    counts = summarize(detections)
    console.print(
        "[dim]"
        f"{counts[Verdict.FLAKY.value]} flaky, "
        f"{counts[Verdict.REGRESSION.value]} regression, "
        f"{counts[Verdict.PERSISTENT_FAILURE.value]} persistent, "
        f"{counts[Verdict.NEW_FAILURE.value]} new, "
        f"{counts[Verdict.HEALTHY.value]} healthy"
        "[/dim]"
    )


def _cause_cell(classification: Classification | None) -> str:
    """One-cell rendering of a proposed cause."""
    if classification is None:
        return "[dim]-[/dim]"
    if classification.is_abstention:
        return f"[dim]UNKNOWN ({classification.downgrade_reason.value})[/dim]"
    return f"{classification.cause.value} [dim]{classification.confidence:.0%}[/dim]"


def _print_classification(console: Console, classification: Classification) -> None:
    """A proposed cause is labelled as a proposal, per ADR-0001.

    The wording matters more than it looks: a cause printed without qualification
    in the same table as a deterministic verdict reads as an equally solid finding,
    which is exactly the conflation this project exists to avoid.
    """
    if classification.is_abstention:
        detail = f": {classification.reasoning}" if classification.reasoning else ""
        console.print(
            f"  - [dim]cause: UNKNOWN ({classification.downgrade_reason.value}){detail}[/dim]"
        )
        return
    console.print(
        f"  - proposed cause: {classification.cause.value} "
        f"(model confidence {classification.confidence:.0%}, advisory only)"
    )
    if classification.reasoning:
        console.print(f"    {classification.reasoning}")
    for item in classification.evidence:
        console.print(f"    evidence: {item}")
    if classification.suggested_action:
        console.print(f"    suggested: {classification.suggested_action}")


def _markdown_cause(classification: Classification | None) -> str:
    if classification is None:
        return "-"
    if classification.is_abstention:
        return f"UNKNOWN (`{classification.downgrade_reason.value}`)"
    return f"`{classification.cause.value}` {classification.confidence:.0%}"


def render_markdown(
    detections: Sequence[Detection],
    *,
    max_rows: int = 10,
    llm_enabled: bool = False,
    classifications: dict[int, Classification] | None = None,
) -> str:
    """Markdown suitable for a PR comment.

    Output is ASCII-only. It is written to a pipe, a file, or the GitHub API,
    none of which is guaranteed to be reading UTF-8 on a Windows runner, and a
    mojibake em dash in a bot comment is a needless way to look broken.

    Brevity is functional, not stylistic: a bot that writes an essay on every
    failed build gets muted, and a muted bot has no effect on anything. The table
    is capped and the detail goes into a collapsed block.
    """
    rows = [
        detection
        for detection in sort_for_report(detections)
        if detection.verdict is not Verdict.HEALTHY
    ]

    # A stable marker so the Action can find and *update* its previous comment
    # instead of adding another. A bot that posts a fresh comment on every push is
    # a bot that gets muted, and a muted bot has no effect on anything.
    lines: list[str] = [COMMENT_MARKER, "### Flaky test triage", ""]

    if not rows:
        lines.append("No flakes, regressions or unexplained failures were detected.")
        return "\n".join(lines) + "\n"

    flaky = [d for d in rows if d.verdict is Verdict.FLAKY]
    regressions = [d for d in rows if d.verdict is Verdict.REGRESSION]

    # The summary line is the only part most readers will read, so it says the
    # one thing they need: is this their fault?
    if regressions:
        lines.append(
            f"**{len(regressions)} failure(s) look like real regressions** introduced by a "
            "change, not flakes. These are not eligible for quarantine."
        )
    if flaky and not regressions:
        lines.append(
            f"**These {len(flaky)} failure(s) appear unrelated to your change** -- they are "
            "known-flaky tests."
        )
    elif flaky:
        lines.append(f"{len(flaky)} further failure(s) appear unrelated to your change.")
    lines.append("")

    resolved = classifications or {}
    if resolved:
        lines.append("| verdict | confidence | flake rate | proposed cause | test |")
        lines.append("|---|---|---|---|---|")
    else:
        lines.append("| verdict | confidence | flake rate | test |")
        lines.append("|---|---|---|---|")
    for detection in rows[:max_rows]:
        name = detection.identity.display_name
        if detection.merged_uncertain:
            name += " _(merged_uncertain)_"
        rate = f"{detection.flake_rate:.0%}" if detection.observations else "-"
        cells = [detection.verdict.value, detection.confidence.value, rate]
        if resolved:
            cells.append(_markdown_cause(resolved.get(detection.identity_id)))
        cells.append(f"`{name}`")
        lines.append("| " + " | ".join(cells) + " |")

    if len(rows) > max_rows:
        lines.append("")
        lines.append(f"_{len(rows) - max_rows} further finding(s) omitted._")

    lines.extend(["", "<details>", "<summary>Evidence</summary>", ""])
    for detection in rows[:max_rows]:
        lines.append(f"**`{detection.identity.display_name}`** -- {detection.verdict.value}")
        for evidence in detection.signals:
            lines.append(f"- `{evidence.signal.value}`: {evidence.detail}")
        if detection.regression_sha:
            lines.append(f"- pivots on commit `{detection.regression_sha[:12]}`")
        if not detection.signals and not detection.regression_sha:
            lines.append("- no flake signal fired; insufficient history to classify")
        classification = resolved.get(detection.identity_id)
        if classification is not None and not classification.is_abstention:
            lines.append(
                f"- proposed cause `{classification.cause.value}` "
                f"({classification.confidence:.0%} model confidence, advisory only): "
                f"{classification.reasoning}"
            )
            for item in classification.evidence:
                lines.append(f"  - evidence: {item}")
            if classification.suggested_action:
                lines.append(f"  - suggested: {classification.suggested_action}")
        elif classification is not None:
            lines.append(
                f"- cause `UNKNOWN` (`{classification.downgrade_reason.value}`); "
                "no cause is proposed"
            )
        lines.append("")
    if not llm_enabled:
        lines.append("_Deterministic detection only; the cause classifier was not run._")
        lines.append("")
    else:
        lines.append(
            "_Causes are proposed by a model and are advisory. Quarantine decisions "
            "are made by deterministic policy._"
        )
        lines.append("")
    lines.extend(["</details>", ""])

    return "\n".join(lines)
