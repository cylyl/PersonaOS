"""Model pricing registry and financial calculations.

Per ADR 0029 and docs/specs/task-telemetry-quality-latency-cost.md:
Tracks token costs per million tokens across LLM models and providers.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """Pricing rate per million tokens (USD)."""

    input_per_million: float
    output_per_million: float


# Default market reference pricing rates (USD per million tokens)
DEFAULT_MODEL_PRICES: dict[str, ModelPrice] = {
    # OpenAI
    "gpt-4o": ModelPrice(input_per_million=2.50, output_per_million=10.00),
    "gpt-4o-mini": ModelPrice(input_per_million=0.15, output_per_million=0.60),
    "o1": ModelPrice(input_per_million=15.00, output_per_million=60.00),
    "o3-mini": ModelPrice(input_per_million=1.10, output_per_million=4.40),
    # Anthropic
    "claude-3-5-sonnet": ModelPrice(input_per_million=3.00, output_per_million=15.00),
    "claude-3-5-haiku": ModelPrice(input_per_million=0.80, output_per_million=4.00),
    "claude-3-opus": ModelPrice(input_per_million=15.00, output_per_million=75.00),
    # Google
    "gemini-1.5-pro": ModelPrice(input_per_million=3.50, output_per_million=10.50),
    "gemini-1.5-flash": ModelPrice(input_per_million=0.075, output_per_million=0.30),
    "gemini-2.0-flash": ModelPrice(input_per_million=0.10, output_per_million=0.40),
    # Open / Local
    "llama-3.1-70b": ModelPrice(input_per_million=0.90, output_per_million=0.90),
    "llama-3.1-8b": ModelPrice(input_per_million=0.20, output_per_million=0.20),
}

DEFAULT_FALLBACK_PRICE = ModelPrice(input_per_million=1.00, output_per_million=3.00)


class ModelPricingRegistry:
    """Registry maintaining pricing models and computing token costs."""

    def __init__(
        self,
        custom_rates: dict[str, ModelPrice] | None = None,
        fallback_price: ModelPrice | None = None,
    ) -> None:
        self._rates: dict[str, ModelPrice] = dict(DEFAULT_MODEL_PRICES)
        if custom_rates:
            self._rates.update(custom_rates)
        self._fallback = fallback_price or DEFAULT_FALLBACK_PRICE

    def get_price(self, model_name: str | None) -> ModelPrice:
        """Resolve pricing for a model by exact name or prefix."""
        if not model_name:
            return self._fallback

        clean_name = model_name.strip().lower()
        if clean_name in self._rates:
            return self._rates[clean_name]

        # Check prefix match (e.g. 'claude-3-5-sonnet-20241022' matches 'claude-3-5-sonnet')
        for key, price in self._rates.items():
            if clean_name.startswith(key):
                return price

        return self._fallback

    def calculate_cost(
        self,
        model_name: str | None,
        input_tokens: int,
        output_tokens: int,
    ) -> float:
        """Calculate total USD cost for consumed input and output tokens."""
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Token counts cannot be negative")

        price = self.get_price(model_name)
        input_cost = (input_tokens / 1_000_000.0) * price.input_per_million
        output_cost = (output_tokens / 1_000_000.0) * price.output_per_million
        return round(input_cost + output_cost, 6)
