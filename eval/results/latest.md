# Evaluation results

Generated 2026-07-30 01:45 UTC from corpus version 1.0.0.

> **The corpus is synthetic and hand-labelled.** These figures are indicative of behaviour on realistic-looking inputs, not production-validated. No number here should be read as an accuracy claim about a real test suite.

49 examples across 9 classes; 13 constructed adversarially (a real regression that presents as a flake, an infra failure whose trace is full of thread names, cases where UNKNOWN is the only correct answer).

## Headline

| metric | baseline | LLM |
|---|---|---|
| **dangerous-error rate** (REAL_REGRESSION called a flake) | 20.0% | 0.0% |
| overall accuracy | 77.6% | 91.8% |
| macro F1 | 0.756 | 0.915 |
| abstention rate (predicted UNKNOWN) | 32.7% | 12.2% |
| accuracy when answering | 93.9% | 90.7% |
| accuracy on adversarial cases | 46.2% | 84.6% |

The dangerous-error rate is first because it is the only metric here with an asymmetric cost. Every other error wastes a reader's time; this one tells an engineer to ignore a real bug.

## Per-class precision and recall

Per-class rather than overall, because the classes are imbalanced: a classifier that never predicts REAL_REGRESSION can still post a respectable accuracy number.

| class | n | baseline P | baseline R | LLM P | LLM R |
|---|---|---|---|---|---|
| `RACE_CONDITION` | 6 | 100% | 33% | 86% | 100% |
| `TIMING_DEPENDENCY` | 5 | 71% | 100% | 83% | 100% |
| `TEST_ORDER_DEPENDENCY` | 5 | 100% | 100% | 100% | 100% |
| `EXTERNAL_DEPENDENCY` | 6 | 100% | 100% | 100% | 100% |
| `SHARED_STATE_LEAK` | 5 | 100% | 80% | 100% | 100% |
| `RESOURCE_EXHAUSTION` | 4 | 100% | 100% | 67% | 100% |
| `INFRA_FLAKE` | 5 | 100% | 100% | 100% | 60% |
| `REAL_REGRESSION` | 5 | 0% | 0% | 100% | 100% |
| `UNKNOWN` | 8 | 44% | 88% | 100% | 75% |

## Where the baseline wins

These are the classes where the expensive model is not earning its cost, and they are worth more than the aggregate figures: they say which causes could be routed to a keyword rule and never sent to a model at all.

- `TEST_ORDER_DEPENDENCY`: baseline F1 1.00 vs LLM 1.00. A keyword list is sufficient here.
- `EXTERNAL_DEPENDENCY`: baseline F1 1.00 vs LLM 1.00. A keyword list is sufficient here.
- `RESOURCE_EXHAUSTION`: baseline F1 1.00 vs LLM 0.80. A keyword list is sufficient here.
- `INFRA_FLAKE`: baseline F1 1.00 vs LLM 0.75. A keyword list is sufficient here.
- `UNKNOWN`: the LLM recalls only 75% where the baseline reaches 88%.

## Confusion matrix

LLM classifier. Rows are ground truth, columns are predictions.

| actual \ predicted | RC | TD | TOD | ED | SSL | RE | IF | RR | U |
|---|---|---|---|---|---|---|---|---|---|
| **RC** | 6 | . | . | . | . | . | . | . | . |
| **TD** | . | 5 | . | . | . | . | . | . | . |
| **TOD** | . | . | 5 | . | . | . | . | . | . |
| **ED** | . | . | . | 6 | . | . | . | . | . |
| **SSL** | . | . | . | . | 5 | . | . | . | . |
| **RE** | . | . | . | . | . | 4 | . | . | . |
| **IF** | . | . | . | . | . | 2 | 3 | . | . |
| **RR** | . | . | . | . | . | . | . | 5 | . |
| **U** | 1 | 1 | . | . | . | . | . | . | 6 |

Codes are initials: `RC` = RACE_CONDITION, `TD` = TIMING_DEPENDENCY, `TOD` = TEST_ORDER_DEPENDENCY, `ED` = EXTERNAL_DEPENDENCY, `SSL` = SHARED_STATE_LEAK, `RE` = RESOURCE_EXHAUSTION, `IF` = INFRA_FLAKE, `RR` = REAL_REGRESSION, `U` = UNKNOWN.

## Why the classifier abstained

Every abstention carries a machine-readable reason, so a low coverage number can be diagnosed rather than merely observed.

| reason | count |
|---|---|
| `model_abstained` | 4 |
| `prefiltered` | 2 |

## Cost and latency

| metric | value |
|---|---|
| API calls | 99 |
| cache hit rate, this (cold) pass | 0.0% |
| cache hit rate on an immediate re-run | 100.0% |
| prefiltered (skipped the expensive model) | 2 |
| schema failures | 3 |
| repairs that succeeded | 3 |
| total cost | $0.6860 |
| mean cost per example | $0.0140 |
| P50 classify latency | 5138 ms |
| P95 classify latency | 9080 ms |

Classifier `claude-sonnet-5`, gate `claude-haiku-4-5-20251001`, prompt `2026-07-30.1+908caee72aa0`, confidence floor 0.55.

Prices come from `flaketriage.toml` and should be checked against the current published list before being quoted.

## Reading this honestly

- The corpus is synthetic. The most likely way these numbers mislead is that hand-written examples are cleaner than real failures: real traces are longer, noisier, and more often ambiguous.
- The author of the corpus also wrote the prompt. Some of the classifier's advantage may be shared vocabulary rather than shared reasoning.
- 49 examples is small. A single reclassified example moves overall accuracy by about two points, so differences of a few points are not meaningful.
- The baseline is a genuine attempt, not a straw man. Where it matches the LLM, the LLM is not earning its cost on that class.

Regenerate with `make eval`. Corpus: `python eval/generate_corpus.py`.
