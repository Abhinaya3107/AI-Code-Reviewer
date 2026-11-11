"""Rough cost estimates. Prices are approximate list prices: check the provider's pricing page."""

# USD per 1M tokens: (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Estimated cost in USD, or None when the model has no known price (including the fake model)."""
    price = _PRICING.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000
