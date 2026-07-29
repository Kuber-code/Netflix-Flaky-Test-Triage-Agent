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


def to_dict(detection: Detection) -> dict[str, Any]:
    """One detection as a JSON-serializable record."""
    return {
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


def render_json(detections: Sequence[Detection], *, llm_enabled: bool = False) -> str:
    payload = {
        "schema_version": 1,
        "llm_enabled": llm_enabled,
        "summary": summarize(detections),
        "detections": [to_dict(detection) for detection in sort_for_report(detections)],
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
    detections: Sequence[Detection], console: Console, *, show_healthy: bool = False
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

    table = Table(box=None, pad_edge=False)
    table.add_column("verdict")
    table.add_column("conf")
    table.add_column("rate", justify="right")
    table.add_column("obs", justify="right")
    table.add_column("test", overflow="fold")

    for detection in rows:
        style = _VERDICT_STYLE.get(detection.verdict, "")
        name = detection.identity.display_name
        if detection.merged_uncertain:
            name += " [dim](merged_uncertain)[/dim]"
        table.add_row(
            f"[{style}]{detection.verdict.value}[/{style}]" if style else detection.verdict.value,
            f"[{_CONFIDENCE_STYLE[detection.confidence]}]{detection.confidence.value}[/]"
            if _CONFIDENCE_STYLE[detection.confidence]
            else detection.confidence.value,
            f"{detection.flake_rate:.0%}" if detection.observations else "-",
            str(detection.observations),
            name,
        )

    console.print(table)
    console.print()

    for detection in rows:
        if not detection.signals:
            continue
        console.print(f"[bold]{detection.identity.display_name}[/bold]")
        for evidence in detection.signals:
            console.print(f"  - {evidence.signal.value}: {evidence.detail}")
        if detection.regression_sha:
            console.print(
                f"  - regression pivots on {detection.regression_sha[:12]}; "
                "not eligible for quarantine"
            )
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


def render_markdown(
    detections: Sequence[Detection], *, max_rows: int = 10, llm_enabled: bool = False
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

    lines: list[str] = ["### Flaky test triage", ""]

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

    lines.append("| verdict | confidence | flake rate | test |")
    lines.append("|---|---|---|---|")
    for detection in rows[:max_rows]:
        name = detection.identity.display_name
        if detection.merged_uncertain:
            name += " _(merged_uncertain)_"
        rate = f"{detection.flake_rate:.0%}" if detection.observations else "-"
        lines.append(
            f"| {detection.verdict.value} | {detection.confidence.value} | {rate} | `{name}` |"
        )

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
        lines.append("")
    if not llm_enabled:
        lines.append("_Deterministic detection only; the cause classifier was not run._")
        lines.append("")
    lines.extend(["</details>", ""])

    return "\n".join(lines)
