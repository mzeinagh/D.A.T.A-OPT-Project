"""Best-effort, overridable per-model USD pricing for cost estimation.

There is no live pricing API to query this programmatically, and prices
change. `estimate_cost_usd()` feeds `LLMResult.estimated_cost_usd`, and
from there `LLMCallLog.estimated_cost_usd` / `PipelineRun.estimated_cost_usd`
(Phase 3) — all of it is exactly what the name says: an *estimate* for
budgeting and observability, never an authoritative billing figure. Verify
against OpenAI's current pricing before relying on this for anything
budget-critical, and update the table below when prices change — it's a
plain dict specifically so that's a one-line edit, not a code change.

Base "gpt-5" pricing below ($1.25 / $10.00 per 1M input/output tokens) is
sourced from public pricing roundups as of August 2026 (OpenAI's own
pricing page was not reachable from this environment to confirm directly —
see the migration plan's blockers list). Treat it as a starting point to
verify, not a guarantee.
"""

# {model: (usd_per_million_input_tokens, usd_per_million_output_tokens)}
PRICING_USD_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "gpt-5": (1.25, 10.00),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Returns None — not 0.0 — for a model this table doesn't know about.

    A silent $0.00 would read as "this call was free"; None makes "we
    don't know the price of this model" visible instead of misleading a
    cost report.
    """
    pricing = PRICING_USD_PER_MILLION_TOKENS.get(model)
    if pricing is None:
        return None
    input_rate, output_rate = pricing
    return (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate
