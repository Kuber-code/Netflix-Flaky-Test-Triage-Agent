"""Token cost accounting.

Prices live in ``flaketriage.toml``, not here. Published prices change, and a
number baked into source is a number nobody checks; a number in a tracked config
file with a comment saying where it came from is one somebody can verify against
the current price list.

An unknown model costs zero rather than raising. A cost figure of 0.00 for a model
with no configured price is visibly wrong in the report, whereas a crash mid-run
loses the classifications already paid for.
"""

from __future__ import annotations

from typing import Final

from flaketriage.obs import get_logger

log = get_logger(__name__)

_PER_MILLION: Final = 1_000_000


class CostTable:
    """Per-model input/output prices in USD per million tokens."""

    def __init__(self, prices: dict[str, tuple[float, float]]) -> None:
        self._prices = {name.lower(): value for name, value in prices.items()}
        self._warned: set[str] = set()

    def price_for(self, model: str) -> tuple[float, float] | None:
        key = model.lower()
        if key in self._prices:
            return self._prices[key]
        # Fall back to a prefix match so a dated snapshot id resolves to its
        # family price rather than silently costing nothing.
        for name, value in self._prices.items():
            if key.startswith(name) or name.startswith(key):
                return value
        return None

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int) -> float:
        price = self.price_for(model)
        if price is None:
            if model not in self._warned:
                self._warned.add(model)
                log.warning("model_price_unknown", model=model)
            return 0.0
        input_price, output_price = price
        return (input_tokens * input_price + output_tokens * output_price) / _PER_MILLION

    def known_models(self) -> tuple[str, ...]:
        return tuple(sorted(self._prices))


def cost_table_from_config(prices: dict[str, list[float]]) -> CostTable:
    """Build a table from the ``[classify.prices]`` config section."""
    parsed: dict[str, tuple[float, float]] = {}
    for model, pair in prices.items():
        if len(pair) != 2:
            log.warning("model_price_malformed", model=model)
            continue
        parsed[model] = (float(pair[0]), float(pair[1]))
    return CostTable(parsed)
