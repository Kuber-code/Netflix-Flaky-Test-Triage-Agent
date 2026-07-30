# Documentation index

Four kinds of document, kept apart on purpose so that none of them has to be read
to understand another.

| Document | Answers |
|---|---|
| [../README.md](../README.md) | What it does, why it is built this way, and what the numbers are |
| [../ARCHITECTURE.md](../ARCHITECTURE.md) | Structure, data flow, storage schema, failure modes |
| [demo.md](demo.md) | What one full pipeline run actually looks like |
| [adr/](adr/) | Why each contentious decision went the way it did |
| [../eval/results/latest.md](../eval/results/latest.md) | Measured accuracy, cost, and where the baseline wins |

## Architecture decision records

Read in this order if you are reading all of them: 0001 is the spine and the rest
are consequences of it.

| ADR | Decision | The uncomfortable part |
|---|---|---|
| [0001](adr/0001-deterministic-core-llm-advisory.md) | Deterministic core, LLM advisory | Most of the codebase would not exist in a prompt-wrapper design |
| [0002](adr/0002-test-identity-strategy.md) | Normalized identity plus labelled aliasing | Aliasing is heuristic and can merge two distinct tests |
| [0003](adr/0003-abstention-over-guessing.md) | Abstention over guessing | Some genuinely classifiable failures are refused |
| [0004](adr/0004-quarantine-ttl.md) | Quarantine carries TTL, owner, exit condition | Recommendations accumulate if nobody acts on them |
| [0005](adr/0005-two-tier-model-cost-strategy.md) | Two-tier models and cost controls | Temperature-0 reproducibility is not available on the configured model |

Each ADR has a "Consequences" section listing what the decision costs, not only
what it buys. That is the section worth reading if you are deciding whether to
trust the rest.

## Gates

| Gate | Enforces |
|---|---|
| `make check` | ruff, `mypy --strict`, the full test suite |
| `make cov` | per-package coverage floor on the deterministic core |
| `make eval-baseline` | the corpus is reproducible and the results table is intact |
| `.github/workflows/self-triage.yml` | the Action actually runs, on this repo's own tests |

Each exists because the corresponding claim in the README would otherwise be
unverified. The per-package coverage gate is the clearest example: the number was
true when checked by hand, which is not the same as being enforced.

## Where the specification lives

[flaky-triage-agent-REQUIREMENTS.md](../flaky-triage-agent-REQUIREMENTS.md) is the
build specification this repository was written against, kept in the repository so
that the finished work can be checked against what was asked for. Section 9 lists
the phases, section 11 the acceptance criteria, and section 13 the things that were
deliberately not built.
