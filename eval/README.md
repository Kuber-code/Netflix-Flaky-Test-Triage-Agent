# Evaluation harness

The most important directory in this repository. Populated in phase P5.

Planned contents:

| Path | Purpose |
|---|---|
| `generate_corpus.py` | Synthesizes realistic JUnit XML and stack traces per cause category |
| `dataset/` | Versioned labeled examples: input context, ground-truth cause, one-line rationale |
| `baseline.py` | Keyword-heuristic classifier the LLM must beat, reported side by side |
| `run_eval.py` | Computes per-class precision/recall, confusion matrix, abstention rate, dangerous-error rate, cost, latency, cache hit rate |
| `results/latest.md` | Committed results table, referenced from the README |

The corpus will be synthetic and hand-labeled. That is a real limitation and is
stated as such in the README rather than glossed over: the figures indicate
behaviour on realistic-looking inputs, not production-validated accuracy.
