"""Evaluation harness.

Runs the keyword baseline and the LLM classifier over the labelled corpus and
writes ``eval/results/latest.md``. Without a key the baseline still runs and the
LLM column is reported as not run, rather than left blank as though it had scored
zero.

The classifier is built through the same ``build_classifier`` the CLI uses. A
harness that assembled its own would be measuring a configuration nobody runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import baseline
import metrics
from flaketriage.classify import build_classifier
from flaketriage.classify.classifier import Classifier
from flaketriage.config import Config, api_key_from_env, load_config
from flaketriage.detect.models import Confidence, Detection, FlakeSignal, SignalEvidence, Verdict
from flaketriage.identity.fingerprint import fingerprint
from flaketriage.models import CauseCode, DiffSummary, FileChange, Outcome, TestIdentity
from paths import DATASET_DIR, LATEST_RESULTS, ensure_dirs


def load_corpus() -> dict[str, Any]:
    path = DATASET_DIR / "corpus.json"
    if not path.is_file():
        raise SystemExit(f"corpus not found at {path}; run python eval/generate_corpus.py first")
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def build_detection(item: dict[str, Any]) -> Detection:
    """Rebuild the Detection the classifier would have been handed in production."""
    history = item["history"]
    suite_path = item["suite_path"]
    file_path = suite_path.split("::")[0]

    identity = TestIdentity(
        fingerprint=fingerprint(suite_path, item["test_name"]),
        suite_path=suite_path,
        test_name=item["test_name"],
        file_path=file_path,
    )
    signals = tuple(
        SignalEvidence(
            signal=FlakeSignal(name),
            detail=f"{name} fired for this test",
            observations=history["observations"],
        )
        for name in history["signals"]
    )
    footprint = {file_path}
    if item["stack_trace"]:
        from flaketriage.detect.footprint import extract_paths

        footprint.update(extract_paths(item["stack_trace"]))

    return Detection(
        identity=identity,
        identity_id=abs(hash(item["id"])) % 1_000_000,
        verdict=Verdict(history["verdict"]),
        confidence=Confidence(history["detector_confidence"]),
        signals=signals,
        flake_rate=history["flake_rate"],
        divergence_rate=history["divergence_rate"],
        intermittency_rate=history["intermittency_rate"],
        retry_data_available=history["retry_data_available"],
        observations=history["observations"],
        windows=history["windows"],
        infra_excluded=history["infra_excluded"],
        latest_outcome=Outcome(item["outcome"]),
        latest_sha="e" * 12,
        failure_message=item["failure_message"],
        failure_type=item["failure_type"],
        stack_trace=item["stack_trace"],
        footprint=tuple(sorted(footprint)),
        failing_shards=tuple(history["failing_shards"]),
        merged_uncertain=history["merged_uncertain"],
    )


def diff_for(item: dict[str, Any]) -> DiffSummary | None:
    paths = item.get("diff_paths") or []
    if not paths:
        return None
    return DiffSummary(files=tuple(FileChange(path=path) for path in paths))


def run_baseline(items: list[dict[str, Any]]) -> metrics.Report:
    predictions = [
        baseline.classify(
            failure_type=item["failure_type"],
            failure_message=item["failure_message"],
            stack_trace=item["stack_trace"],
            failing_shards=tuple(item["history"]["failing_shards"]),
        )
        for item in items
    ]
    return metrics.score(
        "keyword baseline",
        [CauseCode(item["label"]) for item in items],
        predictions,
        adversarial=[item["adversarial"] for item in items],
    )


def run_llm(
    items: list[dict[str, Any]],
    classifier: Classifier,
    *,
    rerun_factory: Callable[[], Classifier] | None = None,
) -> tuple[metrics.Report, dict[str, Any]]:
    """Classify the corpus once cold, then optionally once warm.

    Both passes are needed for an honest cost table. A single run over a populated
    cache reports $0.00 and 100% hits, which is true and useless -- it describes the
    second invocation, not what the tool costs. A single cold run reports the real
    cost and a 0% hit rate, which understates the cache. So the committed table
    carries cold cost and latency alongside the hit rate measured on an immediate
    re-run.
    """
    predictions: list[CauseCode] = []
    reasons: list[str] = []

    for item in items:
        result = classifier.classify(build_detection(item), diff=diff_for(item))
        predictions.append(result.cause)
        reasons.append(result.downgrade_reason.value)

    report = metrics.score(
        "LLM classifier",
        [CauseCode(item["label"]) for item in items],
        predictions,
        adversarial=[item["adversarial"] for item in items],
        downgrade_reasons=reasons,
    )

    rerun_hit_rate: float | None = None
    if rerun_factory is not None:
        warm = rerun_factory()
        for item in items:
            warm.classify(build_detection(item), diff=diff_for(item))
        rerun_hit_rate = warm.stats.cache_hit_rate

    stats = classifier.stats
    classify_latencies = stats.latencies_ms("classify")
    cost_stats = {
        "api_calls": stats.api_calls,
        "cache_hits": stats.cache_hits,
        "cache_hit_rate": stats.cache_hit_rate,
        "rerun_cache_hit_rate": rerun_hit_rate,
        "prefiltered": stats.prefiltered,
        "schema_failures": stats.schema_failures,
        "repairs_succeeded": stats.repairs_succeeded,
        "total_cost_usd": stats.total_cost_usd,
        "cost_per_example_usd": stats.total_cost_usd / len(items) if items else 0.0,
        "p50_latency_ms": metrics.percentile(classify_latencies, 0.50),
        "p95_latency_ms": metrics.percentile(classify_latencies, 0.95),
        "prompt_version": classifier.prompt_version,
    }
    return report, cost_stats


def confusion_table(report: metrics.Report, labels: list[CauseCode]) -> list[str]:
    header = "| actual \\ predicted | " + " | ".join(_short(code) for code in labels) + " |"
    divider = "|---" * (len(labels) + 1) + "|"
    rows = [header, divider]
    for truth in labels:
        cells = [str(report.confusion.get((truth, predicted), 0)) or "." for predicted in labels]
        cells = ["." if cell == "0" else cell for cell in cells]
        rows.append(f"| **{_short(truth)}** | " + " | ".join(cells) + " |")
    return rows


def _short(code: CauseCode) -> str:
    return "".join(part[0] for part in code.value.split("_"))


def _where_baseline_wins(
    baseline_report: metrics.Report, llm_report: metrics.Report, labels: list[CauseCode]
) -> list[str]:
    """Report the classes where the cheap thing does as well or better.

    Knowing where a keyword list suffices is more useful than an aggregate win:
    it says which classes do not need to be paid for.
    """
    wins: list[str] = []
    for code in labels:
        base = baseline_report.per_class[code]
        llm = llm_report.per_class[code]
        if not base.support:
            continue
        if base.f1 >= llm.f1 and base.f1 > 0.0:
            wins.append(
                f"- `{code.value}`: baseline F1 {base.f1:.2f} vs LLM {llm.f1:.2f}. "
                "A keyword list is sufficient here."
            )
        elif llm.recall < 0.8 and base.recall >= llm.recall:
            wins.append(
                f"- `{code.value}`: the LLM recalls only {llm.recall:.0%} where the "
                f"baseline reaches {base.recall:.0%}."
            )

    lines = ["", "## Where the baseline wins", ""]
    if not wins:
        lines.append(
            "On this corpus the LLM matched or beat the baseline on every class with "
            "support. That is a statement about a 49-example synthetic corpus, not a "
            "general result."
        )
        return lines

    lines.append(
        "These are the classes where the expensive model is not earning its cost, "
        "and they are worth more than the aggregate figures: they say which causes "
        "could be routed to a keyword rule and never sent to a model at all."
    )
    lines.append("")
    lines.extend(wins)
    return lines


def render_results(
    corpus: dict[str, Any],
    baseline_report: metrics.Report,
    llm_report: metrics.Report | None,
    cost_stats: dict[str, Any] | None,
    config: Config,
) -> str:
    items = corpus["examples"]
    labels = [code for code in CauseCode if baseline_report.per_class[code].support]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["label"]] = counts.get(item["label"], 0) + 1

    lines = [
        "# Evaluation results",
        "",
        f"Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC "
        f"from corpus version {corpus['corpus_version']}.",
        "",
        "> **The corpus is synthetic and hand-labelled.** These figures are"
        " indicative of behaviour on realistic-looking inputs, not"
        " production-validated. No number here should be read as an accuracy claim"
        " about a real test suite.",
        "",
        f"{len(items)} examples across {len(counts)} classes; "
        f"{sum(1 for item in items if item['adversarial'])} constructed adversarially "
        "(a real regression that presents as a flake, an infra failure whose trace is "
        "full of thread names, cases where UNKNOWN is the only correct answer).",
        "",
        "## Headline",
        "",
    ]

    if llm_report is None:
        lines += [
            "The LLM classifier was **not run** (no `ANTHROPIC_API_KEY` in the"
            " environment). Only the baseline is reported. The LLM column is absent"
            " rather than zero.",
            "",
        ]

    lines += ["| metric | baseline | LLM |", "|---|---|---|"]

    def row(name: str, base: str, llm: str) -> str:
        return f"| {name} | {base} | {llm} |"

    def fmt(value: float, *, pct: bool = True) -> str:
        return f"{value:.1%}" if pct else f"{value:.3f}"

    def llm_cell(value: float, *, pct: bool = True) -> str:
        """ "not run" rather than a zero when the classifier was skipped."""
        return "not run" if llm_report is None else fmt(value, pct=pct)

    zero = 0.0

    lines += [
        row(
            "**dangerous-error rate** (REAL_REGRESSION called a flake)",
            fmt(baseline_report.dangerous_error_rate),
            llm_cell(llm_report.dangerous_error_rate if llm_report else zero),
        ),
        row(
            "overall accuracy",
            fmt(baseline_report.accuracy),
            llm_cell(llm_report.accuracy if llm_report else zero),
        ),
        row(
            "macro F1",
            fmt(baseline_report.macro_f1, pct=False),
            llm_cell(llm_report.macro_f1 if llm_report else zero, pct=False),
        ),
        row(
            "abstention rate (predicted UNKNOWN)",
            fmt(baseline_report.abstention_rate),
            llm_cell(llm_report.abstention_rate if llm_report else zero),
        ),
        row(
            "accuracy when answering",
            fmt(baseline_report.accuracy_when_answering),
            llm_cell(llm_report.accuracy_when_answering if llm_report else zero),
        ),
        row(
            "accuracy on adversarial cases",
            fmt(baseline_report.adversarial_accuracy),
            llm_cell(llm_report.adversarial_accuracy if llm_report else zero),
        ),
        "",
        "The dangerous-error rate is first because it is the only metric here with an"
        " asymmetric cost. Every other error wastes a reader's time; this one tells"
        " an engineer to ignore a real bug.",
        "",
        "## Per-class precision and recall",
        "",
        "Per-class rather than overall, because the classes are imbalanced: a"
        " classifier that never predicts REAL_REGRESSION can still post a"
        " respectable accuracy number.",
        "",
        "| class | n | baseline P | baseline R | LLM P | LLM R |",
        "|---|---|---|---|---|---|",
    ]

    for code in labels:
        base = baseline_report.per_class[code]
        if llm_report is None:
            llm_p = llm_r = "-"
        else:
            llm = llm_report.per_class[code]
            llm_p, llm_r = f"{llm.precision:.0%}", f"{llm.recall:.0%}"
        lines.append(
            f"| `{code.value}` | {base.support} | {base.precision:.0%} | "
            f"{base.recall:.0%} | {llm_p} | {llm_r} |"
        )

    if llm_report is not None:
        lines += _where_baseline_wins(baseline_report, llm_report, labels)

    lines += ["", "## Confusion matrix", ""]
    if llm_report is not None:
        lines += ["LLM classifier. Rows are ground truth, columns are predictions.", ""]
        lines += confusion_table(llm_report, labels)
    else:
        lines += ["Baseline. Rows are ground truth, columns are predictions.", ""]
        lines += confusion_table(baseline_report, labels)
    lines += [
        "",
        "Codes are initials: "
        + ", ".join(f"`{_short(code)}` = {code.value}" for code in labels)
        + ".",
        "",
    ]

    if llm_report is not None and llm_report.downgrade_reasons:
        lines += [
            "## Why the classifier abstained",
            "",
            "Every abstention carries a machine-readable reason, so a low coverage"
            " number can be diagnosed rather than merely observed.",
            "",
            "| reason | count |",
            "|---|---|",
        ]
        for reason, count in sorted(
            llm_report.downgrade_reasons.items(), key=lambda pair: -pair[1]
        ):
            lines.append(f"| `{reason}` | {count} |")
        lines.append("")

    if cost_stats is not None:
        lines += [
            "## Cost and latency",
            "",
            "| metric | value |",
            "|---|---|",
            f"| API calls | {cost_stats['api_calls']} |",
            f"| cache hit rate, this (cold) pass | {cost_stats['cache_hit_rate']:.1%} |",
            (
                "| cache hit rate on an immediate re-run | "
                + (
                    f"{cost_stats['rerun_cache_hit_rate']:.1%} |"
                    if cost_stats["rerun_cache_hit_rate"] is not None
                    else "not measured |"
                )
            ),
            f"| prefiltered (skipped the expensive model) | {cost_stats['prefiltered']} |",
            f"| schema failures | {cost_stats['schema_failures']} |",
            f"| repairs that succeeded | {cost_stats['repairs_succeeded']} |",
            f"| total cost | ${cost_stats['total_cost_usd']:.4f} |",
            f"| mean cost per example | ${cost_stats['cost_per_example_usd']:.4f} |",
            f"| P50 classify latency | {cost_stats['p50_latency_ms']:.0f} ms |",
            f"| P95 classify latency | {cost_stats['p95_latency_ms']:.0f} ms |",
            "",
            f"Classifier `{config.classify.classifier_model}`, "
            f"gate `{config.classify.prefilter_model}`, "
            f"prompt `{cost_stats['prompt_version']}`, "
            f"confidence floor {config.classify.confidence_floor}.",
            "",
            "Prices come from `flaketriage.toml` and should be checked against the"
            " current published list before being quoted.",
            "",
        ]

    lines += [
        "## Reading this honestly",
        "",
        "- The corpus is synthetic. The most likely way these numbers mislead is that"
        " hand-written examples are cleaner than real failures: real traces are"
        " longer, noisier, and more often ambiguous.",
        "- The author of the corpus also wrote the prompt. Some of the classifier's"
        " advantage may be shared vocabulary rather than shared reasoning.",
        "- 49 examples is small. A single reclassified example moves overall accuracy"
        " by about two points, so differences of a few points are not meaningful.",
        "- The baseline is a genuine attempt, not a straw man. Where it matches the"
        " LLM, the LLM is not earning its cost on that class.",
        "",
        "Regenerate with `make eval`. Corpus: `python eval/generate_corpus.py`.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the flaketriage evaluation harness.")
    parser.add_argument("--subset", help="Restrict to one taxonomy class.")
    parser.add_argument(
        "--no-llm", action="store_true", help="Baseline only; makes zero API calls."
    )
    parser.add_argument("--budget-usd", type=float, default=2.0, help="Ceiling for the whole run.")
    parser.add_argument(
        "--no-cache", action="store_true", help="Ignore the cache (measures cold cost)."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Overwrite the committed results table even without an LLM column.",
    )
    args = parser.parse_args()

    ensure_dirs()
    corpus = load_corpus()
    items = list(corpus["examples"])
    if args.subset:
        wanted = args.subset.strip().upper()
        items = [item for item in items if item["label"] == wanted]
        if not items:
            raise SystemExit(f"no examples labelled {wanted}")

    config = load_config()
    baseline_report = run_baseline(items)

    llm_report: metrics.Report | None = None
    cost_stats: dict[str, Any] | None = None

    if not args.no_llm and api_key_from_env() is not None:
        classifier = build_classifier(
            config, budget_usd=args.budget_usd, use_cache=not args.no_cache
        )

        def fresh() -> Classifier:
            return build_classifier(config, budget_usd=args.budget_usd, use_cache=True)

        llm_report, cost_stats = run_llm(
            items, classifier, rerun_factory=None if args.no_cache else fresh
        )
    elif not args.no_llm:
        print("no ANTHROPIC_API_KEY; running the baseline only", file=sys.stderr)

    text = render_results(corpus, baseline_report, llm_report, cost_stats, config)

    # The committed table is the project's central artifact, so it is only
    # overwritten by a run that could actually produce it. A subset measures a
    # slice, and a baseline-only run has no LLM column -- either would silently
    # replace a complete table with a partial one, which is how a results file
    # quietly becomes wrong. Both print instead.
    if args.subset:
        print(text)
        print("\n(subset run: the committed table was not modified)", file=sys.stderr)
        return 0
    if llm_report is None and not args.write:
        print(text)
        print(
            "\n(the classifier did not run, so the committed table was not modified; "
            "set ANTHROPIC_API_KEY, or pass --write to replace it with baseline-only "
            "results)",
            file=sys.stderr,
        )
        return 0

    LATEST_RESULTS.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {LATEST_RESULTS}")
    print(f"  baseline accuracy      {baseline_report.accuracy:.1%}")
    print(f"  baseline dangerous     {baseline_report.dangerous_error_rate:.1%}")
    if llm_report is not None and cost_stats is not None:
        print(f"  LLM accuracy           {llm_report.accuracy:.1%}")
        print(f"  LLM dangerous          {llm_report.dangerous_error_rate:.1%}")
        print(f"  LLM abstention         {llm_report.abstention_rate:.1%}")
        print(f"  cost                   ${cost_stats['total_cost_usd']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
