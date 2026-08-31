"""Integration-style audit of the full LangGraph pipeline (retrieve ->
retrieve_guide -> augment -> generate -> formatter) with mocked retrieval
(exactly three documents) and a recording fake `LLMClient` — no network,
no Django, no real embedding model. Confirms the whole chain end to end:
retrieval -> augmentation -> generation -> continuation, not just one node
in isolation (see `test_generate_node.py` for the focused unit test).
"""
import pymupdf
import pytest

from core_pipeline.graph import graph
from core_pipeline.schemas import Document, SearchResult
from llm.base import LLMResult


class _FakeVectorStore:
    """Three retrieved pages, deterministic, no embedding model needed —
    stands in for `core_pipeline.search.vector_store.VectorStore`."""

    def __init__(self, documents):
        self._documents = documents
        self.keywords = None

    def search(self, query, **kwargs):
        return [SearchResult(document=doc, score=1.0 - i * 0.1) for i, doc in enumerate(self._documents)]


class _RecordingLLMClient:
    """Records every call site's exact prompt and node_name, and returns a
    distinct canned answer per node — so a wiring bug that crossed the
    three call sites (e.g. generate.py accidentally getting formatter's
    prompt) would show up as a wrong answer landing in the wrong field."""

    def __init__(self):
        self.calls: list[dict] = []

    def invoke(self, prompt, *, node_name=None, max_output_tokens=None):
        self.calls.append({"prompt": prompt, "node_name": node_name})
        responses = {
            "retrieve_guide": "Non",
            "generate": "ORAL: gavage (raw)",
            "formatter": "ORAL: gavage",
        }
        if node_name not in responses:
            raise AssertionError(f"unexpected LLM call site: {node_name!r}")
        return LLMResult(text=responses[node_name], model="fake-model", status="success", input_tokens=1, output_tokens=1)


@pytest.fixture
def three_documents():
    return [
        Document(content="Page 1: the test substance was administered by oral gavage.", metadata={"page_num": 1}),
        Document(content="Page 2: dosing occurred once daily for 28 days.", metadata={"page_num": 2}),
        Document(content="Page 3: no adverse effects were observed at the low dose.", metadata={"page_num": 3}),
    ]


def _tiny_handbook_pdf(tmp_path) -> str:
    """A minimal handbook PDF with one titled section — retrieve_guide.py
    parses `+++Title+++` markers from it. Content doesn't need to match
    anything real here since the fake LLM client always answers "Non"
    for this call site, keeping `guide` deterministically None so the
    test's focus (retrieval -> augmentation -> generation) stays clear of
    the guide-matching branch's own logic."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "+++Exposure Route+++\nSome guidance text.")
    fp = tmp_path / "handbook.pdf"
    doc.save(str(fp))
    doc.close()
    return str(fp)


class TestFullGraphWithMockedRetrieval:
    def test_three_documents_flow_through_augmentation_into_generation(self, three_documents, tmp_path):
        store = _FakeVectorStore(three_documents)
        client = _RecordingLLMClient()
        handbook_fp = _tiny_handbook_pdf(tmp_path)

        initial_state = {
            "intro": "You are a chemical toxicity evaluator.",
            "guidebook_fp": handbook_fp,
            "guide": None,
            "question": "What is the exposure route?",
            "few_shots": "Format your answer as: <CATEGORY>: <ANSWER>.",
            "chats_dir": "",
            "augmented_question": None,
            "context": [],
            "output": None,
            "messages": [],
            "corpus_store": store,
            "summary": None,
            "title_page": None,
            "corrected_output": None,
            "final_input": None,
            "retrieved_pages": None,
            "debugging": False,
            "keywords": None,
            "llm_client": client,
        }

        result_state = graph.invoke(initial_state)

        # 1. Retrieval returned exactly the three mocked documents (no
        #    summary/title_page configured to add on top).
        assert result_state["retrieved_pages"]["length"] == 3
        assert result_state["retrieved_pages"]["page numbers"] == [1, 2, 3]
        assert len(result_state["context"]) == 3

        # 2. Augmentation built the expected context prompt: all three
        #    pages' content, plus the question/intro/few_shots framing.
        augmented = result_state["augmented_question"]
        for doc in three_documents:
            assert doc.content in augmented
        assert "What is the exposure route?" in augmented
        assert "You are a chemical toxicity evaluator." in augmented
        assert "BEGIN EXERPT" in augmented and "END EXERPT" in augmented

        # 3. Generation received EXACTLY that prompt (after the one
        #    deterministic clean_prompt_input step) — not a different one,
        #    not formatter's or retrieve_guide's prompt.
        generate_calls = [c for c in client.calls if c["node_name"] == "generate"]
        assert len(generate_calls) == 1
        assert generate_calls[0]["prompt"] == result_state["final_input"]
        for doc in three_documents:
            assert doc.content in generate_calls[0]["prompt"]

        # 4. The generated answer continued on to the next node
        #    (formatter) and the graph reached the end with a real
        #    formatted answer — generate's and formatter's outputs landed
        #    in their own distinct fields, not overwriting each other.
        assert result_state["output"] == "ORAL: gavage (raw)"
        assert result_state["corrected_output"] == "ORAL: gavage"

        # 5. All three LLM call sites fired, exactly once each, in the
        #    correct order, and were never confused with one another.
        node_names = [c["node_name"] for c in client.calls]
        assert node_names == ["retrieve_guide", "generate", "formatter"]

        # 6. formatter's prompt is its own template — it must not equal
        #    (or contain, as a coincidence of substring overlap) the
        #    generate prompt's excerpt delimiters, confirming the two
        #    calls are genuinely distinct rather than one leaking into
        #    the other.
        formatter_prompt = next(c["prompt"] for c in client.calls if c["node_name"] == "formatter")
        assert formatter_prompt != generate_calls[0]["prompt"]
        assert "BEGIN EXERPT" not in formatter_prompt
        assert "Formatting Rules" in formatter_prompt
