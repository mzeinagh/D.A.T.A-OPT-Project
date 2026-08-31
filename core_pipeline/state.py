"""LangGraph state schema shared by every node.

Ported from the original DATA_Project pipeline with one addition:
`llm_client`. Every node that used to call the module-level `config.llm`
singleton now calls `state['llm_client'].invoke(...)` instead — the caller
(the `studies` app's run service, in Phase 3) constructs the concrete
`llm.openai_client.OpenAIResponsesClient` once per run and injects it here,
so no node imports a provider SDK directly.

`llm_client` is typed against `llm.base.LLMClient`, imported unconditionally
rather than under `TYPE_CHECKING` — LangGraph resolves `GraphState`'s
annotations at runtime (`StateGraph.__init__` calls `get_type_hints()` to
build its channels), so a string/forward-ref-only annotation would raise
`NameError` the moment the graph is built. `llm.base` has no Django import
and no heavy dependency (just `dataclasses`/`typing`), so this doesn't pull
anything unwanted into `core_pipeline`.
"""
from typing import Annotated

from typing_extensions import TypedDict, List
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from llm.base import LLMClient

from .schemas import Document
from .search.vector_store import VectorStore


class GraphState(TypedDict):
    intro: str
    guidebook_fp: str
    guide: str
    question: str
    few_shots: str
    chats_dir: str
    augmented_question: str
    context: List[Document]
    output: str
    messages: Annotated[list[AnyMessage], add_messages]
    corpus_store: VectorStore
    summary: Document
    title_page: Document
    corrected_output: str
    final_input: str
    retrieved_pages: dict
    debugging: bool
    keywords: list[str]
    llm_client: LLMClient
