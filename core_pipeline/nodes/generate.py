"""Calls the LLM to produce a raw answer for the augmented prompt.

Ported from the original DATA_Project pipeline. This is the primary
"answer after augmentation" call: the local Ollama call (`from config
import llm`) is replaced by the injected `state['llm_client']` (GPT-5 via
the OpenAI Responses API in production). Prompt cleaning
(`clean_prompt_input`) and the augmented prompt text itself are unchanged.

A failed/timed-out call no longer raises out of the graph — it's recorded
into `output` as a visible placeholder so the run continues (and, once
Phase 3's usage logging lands, `LLMCallLog` carries the real failure
detail); this mirrors the "record failed API calls clearly" requirement
without adding any Django/database dependency to this module.
"""
from ..state import GraphState
from ..utils import clean_prompt_input


def generate(state:GraphState):

    if state['debugging'] == True:
        print("Generating...")

    augmented_input = state['augmented_question']
    # debugging
    print("Cleaning Prompt Input...")
    final_input = clean_prompt_input(augmented_input)

    result = state['llm_client'].invoke(final_input, node_name="generate")

    if not result.ok:
        if state['debugging'] == True:
            print(f"generate LLM call failed ({result.status}): {result.error_message}")
        output = f"[LLM call failed: {result.status}] {result.error_message or ''}".strip()
    else:
        output = result.text

    return{
        'output':output,
        'final_input':final_input
    }
