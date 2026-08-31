"""Bridges `llm.base.OnCallHook` (Phase 1) to the `studies` data model
(Phase 2): persisting `LLMCallLog` rows, accumulating `PipelineRun` usage
totals, and enforcing an optional per-run cost limit.

Nothing in `llm/` or `core_pipeline` imports this module — it's called
only from `studies/tasks.py`, keeping the dependency direction one-way.
"""
import logging

from django.utils import timezone

from llm.base import LLMClient, LLMResult

from .models import LLMCallLog, PipelineRun

logger = logging.getLogger(__name__)


def handle_llm_call(run: PipelineRun, node_name: str | None, result: LLMResult) -> None:
    """The `on_call` hook passed to the concrete `LLMClient` for a run.

    Persists one `LLMCallLog` row per call (success or failure — decision 2
    requires failed calls to be recorded clearly, not just successes), and
    accumulates onto `run`'s usage/cost totals in place, saving `run` so
    `CostLimitedLLMClient` (below) sees the update immediately within the
    same task/process without a re-fetch.
    """
    LLMCallLog.objects.create(
        run=run,
        node_name=node_name or "",
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        latency_ms=result.latency_ms,
        retry_count=result.retry_count,
        status=result.status,
        error_message=result.error_message or "",
        estimated_cost_usd=result.estimated_cost_usd,
    )

    if result.status != "success":
        # Decision 2: a failed/timed-out call must be visible, not just
        # reflected in an aggregate count — this is the row-level detail
        # LLMCallLog exists for, surfaced in the log stream too.
        logger.warning(
            "Run %s: %s call %s — %s", run.id, node_name or "?", result.status, result.error_message or "",
        )

    run.total_api_calls += 1
    run.total_input_tokens += result.input_tokens
    run.total_output_tokens += result.output_tokens
    run.total_latency_ms += result.latency_ms
    if result.estimated_cost_usd is not None:
        run.estimated_cost_usd = (run.estimated_cost_usd or 0.0) + result.estimated_cost_usd

    run.save(
        update_fields=[
            "total_api_calls",
            "total_input_tokens",
            "total_output_tokens",
            "total_latency_ms",
            "estimated_cost_usd",
        ]
    )


class CostLimitedLLMClient:
    """Wraps a real `LLMClient`, refusing to issue a call once `run`'s
    accumulated `estimated_cost_usd` has reached `run.cost_limit_usd` —
    checked *before* each call, per decision 2, not after. A no-op wrapper
    when `run.cost_limit_enabled` is False (the default), so it's always
    safe to wrap with regardless of whether a limit is configured.

    Relies on `handle_llm_call` updating the same `run` instance in place
    (see above) — this class does not itself write to the database except
    the one `halted_due_to_cost_limit` flip below.
    """

    def __init__(self, inner: LLMClient, run: PipelineRun):
        self._inner = inner
        self._run = run

    def invoke(self, prompt: str, *, node_name: str | None = None, max_output_tokens: int | None = None) -> LLMResult:
        if self._run.cost_limit_enabled and self._run.cost_limit_usd is not None:
            current = self._run.estimated_cost_usd or 0.0
            if current >= self._run.cost_limit_usd:
                if not self._run.halted_due_to_cost_limit:
                    self._run.halted_due_to_cost_limit = True
                    self._run.save(update_fields=["halted_due_to_cost_limit"])
                    logger.warning(
                        "Run %s: cost limit reached ($%.4f >= $%.4f) — halting further LLM calls.",
                        self._run.id, current, self._run.cost_limit_usd,
                    )
                return LLMResult(
                    text="",
                    model=getattr(self._inner, "model", ""),
                    status="failed",
                    error_message=(
                        f"Run halted: estimated cost ${current:.4f} has reached the configured "
                        f"limit ${self._run.cost_limit_usd:.4f}."
                    ),
                )

        return self._inner.invoke(prompt, node_name=node_name, max_output_tokens=max_output_tokens)
