"""Concrete `LLMClient` calling the OpenAI Responses API.

Field/parameter names below were verified against the actually-installed
`openai` SDK (v3.5.0) in this environment — `client.responses.create`'s
signature, `openai.types.responses.Response`'s fields, and the exception
classes in `openai` — rather than assumed from memory, since OpenAI's own
docs site (platform.openai.com) is not reachable from this sandbox (see the
migration plan's blockers list). Re-check against the SDK version actually
pinned in requirements.txt if it's bumped later.

This module takes all configuration through its constructor — it does not
import `django.conf.settings` itself, so it stays usable outside Django
(a script, a test, a future non-Django caller). `llm/factory.py` is what
actually reads Django settings and builds one of these.
"""
import time

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    OpenAI,
    RateLimitError,
)

from .base import LLMResult, OnCallHook
from .pricing import estimate_cost_usd

# Retried with backoff: transient / provider-side. Never retried: anything
# else (bad request, auth, not-found, permission...) — those won't succeed
# on retry and would just burn quota and time for nothing.
_RETRYABLE_EXCEPTIONS = (APITimeoutError, APIConnectionError, RateLimitError)

# Statuses the Responses API can hand back on an HTTP-successful call.
# Only "completed" is an unambiguous full success; "incomplete" (most
# commonly: the response hit max_output_tokens before finishing) still
# carries usable partial text, so it's treated as ok=True with the
# truncation recorded in `metadata` rather than discarded — anything else
# unexpected is treated as a failure.
_INCOMPLETE_STATUS = "incomplete"
_COMPLETED_STATUS = "completed"


class OpenAIResponsesClient:
    """`LLMClient` backed by OpenAI's Responses API.

    Retries are implemented here, not left to the SDK's own retry
    machinery (which is explicitly disabled via `max_retries=0` on the
    underlying `OpenAI` client) — that's what makes `LLMResult.retry_count`
    and `latency_ms` accurate, and lets the retryable/non-retryable split
    match the decision exactly: timeouts, connection errors, and rate
    limits (429) are retried with exponential backoff up to `max_retries`;
    a bad request or an authentication failure fails immediately, since no
    amount of retrying fixes a bad API key or a malformed request.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float = 60.0,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
        on_call: OnCallHook | None = None,
    ):
        if not api_key:
            raise ValueError("OpenAIResponsesClient requires an api_key (OPENAI_API_KEY).")
        if not model:
            raise ValueError("OpenAIResponsesClient requires a model (OPENAI_MODEL).")

        self.model = model
        self.default_max_output_tokens = max_output_tokens
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.backoff_base_seconds = backoff_base_seconds
        self._on_call = on_call

        self._client = OpenAI(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    def invoke(
        self,
        prompt: str,
        *,
        node_name: str | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResult:
        effective_max_output_tokens = max_output_tokens or self.default_max_output_tokens

        start = time.monotonic()
        attempt = 0
        last_error: Exception | None = None

        while True:
            try:
                response = self._client.responses.create(
                    model=self.model,
                    input=prompt,
                    max_output_tokens=effective_max_output_tokens,
                )
            except (AuthenticationError, BadRequestError) as exc:
                result = self._failure(exc, "failed", start, attempt)
                self._notify(node_name, result)
                return result
            except _RETRYABLE_EXCEPTIONS as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    status = "timeout" if isinstance(exc, APITimeoutError) else "failed"
                    result = self._failure(exc, status, start, attempt)
                    self._notify(node_name, result)
                    return result
                self._sleep_backoff(attempt)
                attempt += 1
                continue
            except APIStatusError as exc:
                # 5xx is provider-side and worth retrying; anything else
                # (403/404/etc., not already caught above) is not.
                is_server_error = 500 <= exc.status_code < 600
                if is_server_error and attempt < self.max_retries:
                    last_error = exc
                    self._sleep_backoff(attempt)
                    attempt += 1
                    continue
                result = self._failure(exc, "failed", start, attempt)
                self._notify(node_name, result)
                return result
            else:
                result = self._success(response, start, attempt)
                self._notify(node_name, result)
                return result

        # Unreachable (the loop only exits via return above), but avoids
        # ever silently falling through without a result if that changes.
        result = LLMResult(
            text="",
            model=self.model,
            status="failed",
            latency_ms=(time.monotonic() - start) * 1000,
            retry_count=attempt,
            error_message=f"exhausted retries without a definitive result: {last_error}",
        )
        self._notify(node_name, result)
        return result

    # ------------------------------------------------------------ helpers

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(self.backoff_base_seconds * (2**attempt))

    def _notify(self, node_name: str | None, result: LLMResult) -> None:
        if self._on_call is not None:
            self._on_call(node_name, result)

    def _failure(self, exc: Exception, status: str, start: float, attempt: int) -> LLMResult:
        return LLMResult(
            text="",
            model=self.model,
            status=status,
            latency_ms=(time.monotonic() - start) * 1000,
            retry_count=attempt,
            error_message=f"{type(exc).__name__}: {exc}",
        )

    def _success(self, response, start: float, attempt: int) -> LLMResult:
        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        response_status = getattr(response, "status", None)

        metadata: dict = {}
        status: str
        error_message: str | None = None

        if response_status in (_COMPLETED_STATUS, None):
            # `status` is only populated on some response types; a
            # non-background synchronous call with no error is a plain
            # success either way.
            status = "success"
        elif response_status == _INCOMPLETE_STATUS:
            # Most commonly: max_output_tokens was hit before the model
            # finished. Still has usable partial text, so this stays
            # ok=True — the truncation is recorded for visibility rather
            # than discarding a usable (if cut-off) answer.
            status = "success"
            incomplete_details = getattr(response, "incomplete_details", None)
            reason = getattr(incomplete_details, "reason", None) if incomplete_details else None
            metadata["incomplete"] = True
            metadata["incomplete_reason"] = reason
        else:
            # failed / cancelled / queued / in_progress on what should
            # have been a synchronous, non-background call — treat as a
            # failure rather than guessing at partial text.
            status = "failed"
            error = getattr(response, "error", None)
            error_message = f"response.status={response_status!r} error={error!r}"

        text = getattr(response, "output_text", "") or "" if status == "success" else ""

        return LLMResult(
            text=text,
            model=self.model,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=(time.monotonic() - start) * 1000,
            retry_count=attempt,
            error_message=error_message,
            raw_response_id=getattr(response, "id", None),
            estimated_cost_usd=estimate_cost_usd(self.model, input_tokens, output_tokens),
            metadata=metadata,
        )
