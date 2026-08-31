"""Defines the LangGraph workflow: retrieve -> retrieve_guide -> augment -> generate -> formatter.

Ported unchanged from the original DATA_Project pipeline aside from import
paths — the graph wiring itself is untouched by the GPT-5 migration; only
the LLM call *inside* three of these nodes changed (see `nodes/`).
"""
from langgraph.graph import END, START, StateGraph

from .nodes.augment import augment
from .nodes.formatter import formatter
from .nodes.generate import generate
from .nodes.retrieve import retrieve
from .nodes.retrieve_guide import retrieve_guide
from .state import GraphState


def build_graph():
    builder = StateGraph(GraphState)

    # Nodes
    builder.add_node("retriever_1", retrieve)
    builder.add_node("retrieve_guide_1", retrieve_guide)
    builder.add_node("augment_1", augment)
    builder.add_node("generate_1", generate)
    builder.add_node("formatter", formatter)

    # Edges
    builder.add_edge(START, "retriever_1")
    builder.add_edge("retriever_1", "retrieve_guide_1")
    builder.add_edge("retrieve_guide_1", "augment_1")
    builder.add_edge("augment_1", "generate_1")
    builder.add_edge("generate_1", "formatter")
    builder.add_edge("formatter", END)

    return builder.compile()


graph = build_graph()
