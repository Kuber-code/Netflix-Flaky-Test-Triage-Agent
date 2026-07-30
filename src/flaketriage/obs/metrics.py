"""Run metrics: persistence, aggregation, and Prometheus rendering.

A log line answers "what happened just now". These tables answer "what does this
tool cost us per week, and how often does it decline to answer" -- which is the
question that decides whether it stays switched on. So model calls and
classifications are persisted, not merely logged, and abstentions are stored
alongside successes: an abstention rate cannot be computed from a table that only
records the times the classifier had something to say.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

from flaketriage.models import Classification

#: Prometheus metric prefix. Fixed rather than configurable: a scrape target
#: whose metric names vary per deployment cannot be aggregated across them.
METRIC_PREFIX: Final = "flaketriage"


class CallMetric:
    """One model call, flattened for storage."""

    __slots__ = (
        "cache_hit",
        "cost_usd",
        "error",
        "input_tokens",
        "kind",
        "latency_ms",
        "model",
        "output_tokens",
        "prompt_version",
        "schema_valid",
    )

    def __init__(
        self,
        *,
        kind: str,
        model: str,
        prompt_version: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_usd: float = 0.0,
        latency_ms: float = 0.0,
        cache_hit: bool = False,
        schema_valid: bool = True,
        error: str | None = None,
    ) -> None:
        self.kind = kind
        self.model = model
        self.prompt_version = prompt_version
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.latency_ms = latency_ms
        self.cache_hit = cache_hit
        self.schema_valid = schema_valid
        self.error = error


class MetricsSummary:
    """Aggregated metrics over a time window, as `stats` reports them."""

    __slots__ = (
        "abstentions",
        "api_calls",
        "by_cause",
        "by_downgrade_reason",
        "cache_hits",
        "classifications",
        "errors",
        "latency_p50_ms",
        "latency_p95_ms",
        "runs",
        "since",
        "total_cost_usd",
        "total_input_tokens",
        "total_output_tokens",
    )

    def __init__(self) -> None:
        self.since: str | None = None
        self.runs = 0
        self.api_calls = 0
        self.cache_hits = 0
        self.errors = 0
        self.total_cost_usd = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.latency_p50_ms = 0.0
        self.latency_p95_ms = 0.0
        self.classifications = 0
        self.abstentions = 0
        self.by_cause: dict[str, int] = {}
        self.by_downgrade_reason: dict[str, int] = {}

    @property
    def abstention_rate(self) -> float:
        return self.abstentions / self.classifications if self.classifications else 0.0

    @property
    def cache_hit_rate(self) -> float:
        """Hits over all classification attempts, cached or not.

        The denominator is attempts rather than API calls: a rate computed over
        calls only would rise as the cache got *worse*, since every miss adds a
        call to the denominator and a hit adds nothing.
        """
        attempts = self.classifications
        return self.cache_hits / attempts if attempts else 0.0

    @property
    def cost_per_classification_usd(self) -> float:
        return self.total_cost_usd / self.classifications if self.classifications else 0.0


def record_calls(
    connection: sqlite3.Connection, calls: Sequence[CallMetric], *, run_pk: int | None = None
) -> int:
    now = _now()
    connection.executemany(
        """
        INSERT INTO llm_calls
            (run_pk, kind, model, prompt_version, input_tokens, output_tokens,
             cost_usd, latency_ms, cache_hit, schema_valid, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                run_pk,
                call.kind,
                call.model,
                call.prompt_version,
                call.input_tokens,
                call.output_tokens,
                call.cost_usd,
                call.latency_ms,
                int(call.cache_hit),
                int(call.schema_valid),
                call.error,
                now,
            )
            for call in calls
        ],
    )
    return len(calls)


def record_classifications(
    connection: sqlite3.Connection,
    classifications: dict[int, Classification],
    *,
    run_pk: int | None = None,
    commit_sha: str = "",
    cache_hits: frozenset[int] = frozenset(),
) -> int:
    now = _now()
    connection.executemany(
        """
        INSERT INTO classifications
            (identity_id, run_pk, commit_sha, cause, confidence, abstained,
             downgrade_reason, reasoning, evidence, suggested_action, model,
             prompt_version, cache_hit, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                identity_id,
                run_pk,
                commit_sha,
                result.cause.value,
                result.confidence,
                int(result.abstained),
                result.downgrade_reason.value,
                result.reasoning or None,
                json.dumps(list(result.evidence)) if result.evidence else None,
                result.suggested_action or None,
                result.model,
                result.prompt_version,
                int(identity_id in cache_hits),
                now,
            )
            for identity_id, result in classifications.items()
        ],
    )
    return len(classifications)


def summarize(connection: sqlite3.Connection, *, since: str | None = None) -> MetricsSummary:
    """Aggregate metrics over calls and classifications created since ``since``."""
    summary = MetricsSummary()
    summary.since = since
    cutoff = since or ""

    row = connection.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(cost_usd), 0) AS cost,
               COALESCE(SUM(input_tokens), 0) AS input_tokens,
               COALESCE(SUM(output_tokens), 0) AS output_tokens,
               COALESCE(SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END), 0) AS errors
          FROM llm_calls
         WHERE created_at >= ?
        """,
        (cutoff,),
    ).fetchone()
    summary.api_calls = int(row["n"])
    summary.total_cost_usd = float(row["cost"])
    summary.total_input_tokens = int(row["input_tokens"])
    summary.total_output_tokens = int(row["output_tokens"])
    summary.errors = int(row["errors"])

    latencies = [
        float(item["latency_ms"])
        for item in connection.execute(
            """
            SELECT latency_ms FROM llm_calls
             WHERE created_at >= ? AND kind = 'classify' AND error IS NULL
             ORDER BY latency_ms
            """,
            (cutoff,),
        )
    ]
    summary.latency_p50_ms = _percentile(latencies, 0.50)
    summary.latency_p95_ms = _percentile(latencies, 0.95)

    row = connection.execute(
        """
        SELECT COUNT(*) AS n,
               COALESCE(SUM(abstained), 0) AS abstained,
               COALESCE(SUM(cache_hit), 0) AS cache_hits
          FROM classifications
         WHERE created_at >= ?
        """,
        (cutoff,),
    ).fetchone()
    summary.classifications = int(row["n"])
    summary.abstentions = int(row["abstained"])
    summary.cache_hits = int(row["cache_hits"])

    for item in connection.execute(
        "SELECT cause, COUNT(*) AS n FROM classifications WHERE created_at >= ? GROUP BY cause",
        (cutoff,),
    ):
        summary.by_cause[str(item["cause"])] = int(item["n"])

    for item in connection.execute(
        """
        SELECT downgrade_reason AS reason, COUNT(*) AS n
          FROM classifications
         WHERE created_at >= ? AND downgrade_reason != 'none'
         GROUP BY downgrade_reason
        """,
        (cutoff,),
    ):
        summary.by_downgrade_reason[str(item["reason"])] = int(item["n"])

    row = connection.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE started_at >= ?", (cutoff,)
    ).fetchone()
    summary.runs = int(row["n"])

    return summary


def render_prometheus(summary: MetricsSummary) -> str:
    """Prometheus text exposition format.

    Included so the tool could be scraped in a real deployment rather than only
    read by a human. Counters are named with their unit, per convention, and the
    per-cause series is labelled rather than exploded into one metric per cause --
    a taxonomy change should not change the metric namespace.
    """
    lines: list[str] = []

    def metric(name: str, kind: str, help_text: str, value: float, labels: str = "") -> None:
        full = f"{METRIC_PREFIX}_{name}"
        if not any(line.startswith(f"# TYPE {full} ") for line in lines):
            lines.append(f"# HELP {full} {help_text}")
            lines.append(f"# TYPE {full} {kind}")
        lines.append(f"{full}{labels} {value:g}")

    metric("runs_total", "counter", "CI runs ingested.", summary.runs)
    metric("llm_calls_total", "counter", "Model calls made.", summary.api_calls)
    metric("llm_call_errors_total", "counter", "Model calls that failed.", summary.errors)
    metric(
        "llm_cost_usd_total",
        "counter",
        "Cumulative model spend in USD.",
        summary.total_cost_usd,
    )
    metric(
        "llm_input_tokens_total", "counter", "Input tokens consumed.", summary.total_input_tokens
    )
    metric(
        "llm_output_tokens_total", "counter", "Output tokens produced.", summary.total_output_tokens
    )
    metric("classifications_total", "counter", "Classifications produced.", summary.classifications)
    metric(
        "abstentions_total",
        "counter",
        "Classifications that declined to assign a cause.",
        summary.abstentions,
    )
    metric(
        "abstention_ratio",
        "gauge",
        "Abstentions over classifications.",
        summary.abstention_rate,
    )
    metric(
        "cache_hit_ratio",
        "gauge",
        "Cache hits over classification attempts.",
        summary.cache_hit_rate,
    )
    metric(
        "llm_latency_ms",
        "gauge",
        "Classification latency in milliseconds.",
        summary.latency_p50_ms,
        '{quantile="0.5"}',
    )
    metric(
        "llm_latency_ms",
        "gauge",
        "Classification latency in milliseconds.",
        summary.latency_p95_ms,
        '{quantile="0.95"}',
    )

    for cause, count in sorted(summary.by_cause.items()):
        metric(
            "classifications_by_cause_total",
            "counter",
            "Classifications grouped by proposed cause.",
            count,
            f'{{cause="{cause}"}}',
        )
    for reason, count in sorted(summary.by_downgrade_reason.items()):
        metric(
            "downgrades_total",
            "counter",
            "Abstentions grouped by reason.",
            count,
            f'{{reason="{reason}"}}',
        )

    return "\n".join(lines) + "\n"


def as_dict(summary: MetricsSummary) -> dict[str, Any]:
    return {
        "since": summary.since,
        "runs": summary.runs,
        "api_calls": summary.api_calls,
        "llm_call_errors": summary.errors,
        "classifications": summary.classifications,
        "abstentions": summary.abstentions,
        "abstention_rate": round(summary.abstention_rate, 4),
        "cache_hits": summary.cache_hits,
        "cache_hit_rate": round(summary.cache_hit_rate, 4),
        "total_cost_usd": round(summary.total_cost_usd, 6),
        "cost_per_classification_usd": round(summary.cost_per_classification_usd, 6),
        "total_input_tokens": summary.total_input_tokens,
        "total_output_tokens": summary.total_output_tokens,
        "latency_p50_ms": round(summary.latency_p50_ms, 1),
        "latency_p95_ms": round(summary.latency_p95_ms, 1),
        "by_cause": dict(sorted(summary.by_cause.items())),
        "by_downgrade_reason": dict(sorted(summary.by_downgrade_reason.items())),
    }


def _percentile(ordered: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile over an already-sorted sequence."""
    if not ordered:
        return 0.0
    index = min(round(fraction * (len(ordered) - 1)), len(ordered) - 1)
    return ordered[index]


def _now() -> str:
    return datetime.now(UTC).isoformat()
