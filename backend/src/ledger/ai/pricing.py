"""Gemini list prices for estimating spend in the AI call log.

USD per 1M tokens, paid tier, standard (non-batch) requests; output prices include thinking tokens.
Source: https://ai.google.dev/gemini-api/docs/pricing (checked 2026-10-17). Update when models or prices change;
calls to models missing here are logged without a cost.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    cached: Decimal
    # Some models bill every token of a call at a higher rate once its prompt exceeds this many tokens
    long_prompt: int | None = None
    long_input: Decimal | None = None
    long_output: Decimal | None = None
    long_cached: Decimal | None = None


def _p(*values) -> Price:
    return Price(*(Decimal(str(v)) if isinstance(v, float) else v for v in values))


# model -> [(effective from, price)], oldest first
PRICES: dict[str, list[tuple[date, Price]]] = {
    "gemini-3.8-flash": [
        (date.min, _p(0.75, 3.75, 0.075)),  # launch promo through 2026-12-31
        (date(2027, 1, 1), _p(1.50, 7.50, 0.15)),
    ],
    "gemini-3.5-flash-lite": [(date.min, _p(0.30, 2.50, 0.03))],
    "gemini-3.1-pro-preview": [(date.min, _p(2.00, 12.00, 0.20, 200_000, 4.00, 18.00, 0.40))],
}

MILLION = Decimal(1_000_000)


def price_for(model: str, on: date) -> Price | None:
    model = model.removeprefix("models/")
    # Longest match so versioned ids (e.g. "...-001") resolve and "flash-lite" isn't priced as "flash"
    key = max((k for k in PRICES if model == k or model.startswith(f"{k}-")), key=len, default=None)
    if key is None:
        return None
    return next(p for start, p in reversed(PRICES[key]) if on >= start)


def estimate_cost(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    thinking_tokens: int | None = None,
    cached_tokens: int | None = None,
    on: date | None = None,
) -> Decimal | None:
    """Estimated USD for one call; `input_tokens` includes `cached_tokens`, as in Gemini's usage metadata."""
    price = price_for(model, on or datetime.now(UTC).date())
    if price is None:
        return None
    prompt, cached = input_tokens or 0, min(cached_tokens or 0, input_tokens or 0)
    rate_in, rate_out, rate_cached = price.input, price.output, price.cached
    if price.long_prompt is not None and prompt > price.long_prompt:
        rate_in = price.long_input or rate_in
        rate_out = price.long_output or rate_out
        rate_cached = price.long_cached or rate_cached
    total = (prompt - cached) * rate_in + cached * rate_cached + ((output_tokens or 0) + (thinking_tokens or 0)) * rate_out
    return (total / MILLION).quantize(Decimal("0.000001"))
