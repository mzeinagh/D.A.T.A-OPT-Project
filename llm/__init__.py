"""Provider-agnostic LLM client abstraction.

`core_pipeline` nodes call whatever `LLMClient` they're handed through
`GraphState['llm_client']` — they never import a concrete provider. This is
what lets the provider (or model) change later without touching a single
pipeline node.

Phase 0 only defines the interface (`base.py`). The concrete
`OpenAIResponsesClient` (OpenAI Responses API, GPT-5) lands in Phase 1,
alongside the retry/timeout/cost-tracking wiring described in the migration
plan.
"""
from .base import LLMClient, LLMResult, OnCallHook

__all__ = ("LLMClient", "LLMResult", "OnCallHook")
