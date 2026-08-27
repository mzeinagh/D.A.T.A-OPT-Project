"""The `LLMClient` interface every pipeline node is written against.

Every one of the three LLM call sites in `core_pipeline.nodes`
(`retrieve_guide`, `generate`, `formatter`) calls `invoke()` on whatever
client is injected into `GraphState['llm_client']` — never a concrete
provider SDK. That's the seam that lets the provider (currently: OpenAI's
Responses API, GPT-5) change later without editing a single node.

`LLMResult` is intentionally a plain dataclass, not a LangChain message
object — the old pipeline's nodes called `.pretty_repr()` / `.text()` on
`ChatOllama`'s LangChain return objects; the ported nodes work with plain
strings instead (see `core_pipeline/nodes/`), and only reconstruct
LangChain `HumanMessage`/`AIMessage` wrappers where `GraphState['messages']`
still requires them for LangGraph's `add_messages` reducer.

This module has no Django import and no provider SDK import — it stays
usable from a plain script or a test, exactly like `core_pipeline`.
"""
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

CallStatus = Literal["success", "failed", "timeout"]

# Invoked by a concrete client after every call (success or failure), with
# the calling node's name and the resulting LLMResult. Optional — nothing
# in Phase 0/1 uses this yet. It exists as the integration point Phase 3's
# run-orchestration service is expected to use to persist an `LLMCallLog`
# row per call and accumulate `PipelineRun` usage/cost totals in real time
# (including checking a per-run cost limit before the *next* call), without
# requiring any further change to `llm/` or the pipeline nodes.
OnCallHook = Callable[[str | None, "LLMResult"], None]


@dataclass
class LLMResult:
    """The outcome of one `LLMClient.invoke()` call.

    `text` is only meaningful when `status == "success"` — callers must
    check `status` before using it. A failed/timed-out call still returns
    an `LLMResult` (never raises) so the orchestrating service can log an
    `LLMCallLog` row and decide whether to halt the run, rather than losing
    the failure to an uncaught exception mid-graph.
    """

    text: str
    model: str
    status: CallStatus
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    retry_count: int = 0
    error_message: str | None = None
    raw_response_id: str | None = None
    estimated_cost_usd: float | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"


class LLMClient(Protocol):
    """Structural interface — a concrete client just needs to implement
    `invoke()` with this shape; it does not need to subclass this Protocol.
    """

    def invoke(
        self,
        prompt: str,
        *,
        node_name: str | None = None,
        max_output_tokens: int | None = None,
    ) -> LLMResult:
        """Send `prompt` to the model and return an `LLMResult`.

        `node_name` identifies the call site (`"retrieve_guide"`,
        `"generate"`, or `"formatter"`) purely for logging/usage
        attribution (`LLMCallLog.node_name`) — it must not change prompt
        content or behavior.

        `max_output_tokens` lets a caller override the client's configured
        default for one call; when omitted, the client applies its own
        configured limit (e.g. `settings.OPENAI_MAX_OUTPUT_TOKENS`).

        Must never raise for ordinary provider failures (timeout, rate
        limit, server error) — those are reported via
        `LLMResult(status="failed"/"timeout", error_message=...)` so the
        caller can log and decide how to proceed. Retries, if any, happen
        inside `invoke()` and are reflected in `retry_count`.
        """
        ...
