"""Focused wiring audit: does the exact prompt `augment.py` builds reach
`generate.py`'s LLM call unchanged (beyond the one pre-existing, legacy-
identical `clean_prompt_input` cleanup)?

No network, no Django — just the plain-Python `core_pipeline` nodes and a
recording fake `LLMClient` that captures the exact `prompt` argument
`generate()` passes to `invoke()`, so this test asserts prompt *identity*
(not just "a response came back"), which the existing task-level tests
(`studies/tests/test_tasks.py`) don't check.
"""
from core_pipeline.nodes.augment import augment
from core_pipeline.nodes.generate import generate
from core_pipeline.utils import clean_prompt_input
from llm.base import LLMResult


class _RecordingLLMClient:
    """Captures every `invoke()` call's exact arguments and returns a
    canned `LLMResult` — never touches the network."""

    def __init__(self, response_text: str = "FAKE ANSWER", status: str = "success", error_message: str | None = None):
        self.response_text = response_text
        self.status = status
        self.error_message = error_message
        self.calls: list[dict] = []

    def invoke(self, prompt, *, node_name=None, max_output_tokens=None):
        self.calls.append({"prompt": prompt, "node_name": node_name, "max_output_tokens": max_output_tokens})
        return LLMResult(
            text=self.response_text if self.status == "success" else "",
            model="fake-model",
            status=self.status,
            input_tokens=1,
            output_tokens=1,
            error_message=self.error_message,
        )


def _base_state(**overrides):
    state = {
        "intro": "You are a chemical toxicity evaluator.",
        "few_shots": "Format your answer as: <CATEGORY>: <ANSWER>.",
        "question": "What is the exposure route?",
        "guide": None,
        "context": ["Page 1 content about oral gavage exposure."],
        "debugging": False,
    }
    state.update(overrides)
    return state


class TestAugmentToGenerateWiring:
    def test_generate_sends_exactly_the_cleaned_augmented_prompt(self):
        state = _base_state()

        augment_result = augment(state)
        assert "augmented_question" in augment_result
        state = {**state, **augment_result}

        client = _RecordingLLMClient(response_text="ORAL: gavage")
        state["llm_client"] = client

        generate_result = generate(state)

        # Exactly one call, and it's attributed to the "generate" node —
        # never confused with retrieve_guide or formatter (see the
        # graph-level test for all three call sites together).
        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["node_name"] == "generate"

        # The prompt actually sent is EXACTLY clean_prompt_input(augmented_question)
        # — not the raw augmented_question, not something else, not truncated.
        expected_prompt = clean_prompt_input(augment_result["augmented_question"])
        assert call["prompt"] == expected_prompt

        # generate.py's own returned final_input (what studies/tasks.py
        # stores as Answer.augmented_prompt) matches that same string.
        assert generate_result["final_input"] == expected_prompt

        # The fake response landed in the correct graph-state field.
        assert generate_result["output"] == "ORAL: gavage"

        # Sanity: the real content actually made it into the prompt —
        # not dropped, not replaced by something else.
        assert "What is the exposure route?" in call["prompt"]
        assert "Page 1 content about oral gavage exposure." in call["prompt"]
        assert "You are a chemical toxicity evaluator." in call["prompt"]
        assert "Format your answer as: <CATEGORY>: <ANSWER>." in call["prompt"]

    def test_prompt_is_not_duplicated_or_wrapped(self):
        """The prompt appears in the API call exactly once, as a plain
        string — never doubled, never nested inside another structure."""
        state = _base_state()
        state = {**state, **augment(state)}
        client = _RecordingLLMClient()
        state["llm_client"] = client

        generate(state)

        prompt = client.calls[0]["prompt"]
        assert isinstance(prompt, str)
        # The excerpt delimiters appear exactly once each — a duplication
        # bug would double these. Note: clean_prompt_input fixes the
        # template's own "EXERPT" typo to "EXCERPT" as part of its
        # cleaning (see its docstring), so the *cleaned* prompt spells it
        # correctly — this isn't a wiring bug, just what the existing,
        # legacy-identical cleanup step does.
        assert prompt.count("BEGIN EXCERPT") == 1
        assert prompt.count("END EXCERPT") == 1
        assert prompt.count("Page 1 content about oral gavage exposure.") == 1

    def test_failed_llm_call_does_not_silently_lose_the_prompt(self):
        """A failed generate call still records the real final_input —
        only `output` becomes a visible failure placeholder (decision 2:
        a failed call must be visible, not silently swallowed)."""
        state = _base_state()
        state = {**state, **augment(state)}
        state["llm_client"] = _RecordingLLMClient(status="failed", error_message="simulated failure")

        result = generate(state)

        assert result["final_input"] == clean_prompt_input(state["augmented_question"])
        assert "LLM call failed" in result["output"]
        assert "simulated failure" in result["output"]

    def test_no_context_produces_the_no_information_prompt(self):
        """The other augment.py branch (empty retrieval) — confirms
        generate still forwards whatever augment.py actually produced,
        not a hardcoded assumption about its shape."""
        state = _base_state(context=[])
        state = {**state, **augment(state)}
        client = _RecordingLLMClient()
        state["llm_client"] = client

        generate(state)

        prompt = client.calls[0]["prompt"]
        assert "no information found" in prompt.lower()
        assert "What is the exposure route?" in prompt
