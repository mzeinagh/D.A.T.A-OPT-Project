"""The one place that connects Django settings to `llm/`'s provider-agnostic
client construction.

`llm/openai_client.py` deliberately takes plain constructor arguments and
has no Django import, so it stays usable from a script or a test outside
any Django context. This module is the (thin, swappable) bridge: it reads
`django.conf.settings` and builds the concrete client. Callers — the
`run_pipeline` management command today, a Celery task in Phase 3 — import
`build_default_llm_client` rather than constructing `OpenAIResponsesClient`
themselves, so switching providers later is a one-function change here,
not a change at every call site.
"""
from django.conf import settings

from .base import LLMClient, OnCallHook
from .openai_client import OpenAIResponsesClient


def build_default_llm_client(*, on_call: OnCallHook | None = None) -> LLMClient:
    """Builds the `LLMClient` configured entirely from Django settings
    (in turn sourced from environment variables — see `.env.example`).

    `on_call`, if given, is threaded straight through to the concrete
    client — see `llm.base.OnCallHook` for what it's for.
    """
    price = None
    if (
        settings.OPENAI_PRICE_INPUT_USD_PER_1M is not None
        and settings.OPENAI_PRICE_OUTPUT_USD_PER_1M is not None
    ):
        price = (settings.OPENAI_PRICE_INPUT_USD_PER_1M, settings.OPENAI_PRICE_OUTPUT_USD_PER_1M)

    return OpenAIResponsesClient(
        api_key=settings.OPENAI_API_KEY,
        model=settings.OPENAI_MODEL,
        max_output_tokens=settings.OPENAI_MAX_OUTPUT_TOKENS,
        timeout_seconds=settings.OPENAI_TIMEOUT_SECONDS,
        max_retries=settings.OPENAI_MAX_RETRIES,
        on_call=on_call,
        base_url=settings.OPENAI_BASE_URL,
        price_usd_per_million=price,
    )
