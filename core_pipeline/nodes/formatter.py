"""Formats/normalizes the raw LLM answer into the <CATEGORY> : <ANSWER> format.

Ported from the original DATA_Project pipeline. The local Ollama call is
replaced by `state['llm_client']`. `output` is now a plain string (see
`generate.py`), so the old `.text()` call on a LangChain message object is
just a direct read; `corrected_output` is likewise a plain string now,
matching what `GraphState` already declared its type as. The `messages`
history still gets proper `HumanMessage`/`AIMessage` wrappers, since
LangGraph's `add_messages` reducer expects them — only the *source* of
their content changed.
"""
from langchain_core.messages import AIMessage, HumanMessage

from ..state import GraphState


def formatter(state:GraphState):

    if state['debugging'] == True:
        print("Formatting...")

    output = state['output']
    question = state['question']
    template = f"""
        You need to read a question and its response, then respond with only the target information from the response.
        \nThe Question:
        \n{question}
        \nThe Response:
        \n{output}

        \nFormatting Rules are as follows:
        \n- Disgard every thing aside from the answer, this includes all thinking processes or justifications for the answer.\n
        \n- Ensure that the final response contains ONLY lines in this EXACT format: <category> : <information>.\n
        \n
        \nExample of Acceptable Outputs:
        \nDERMAL : Sensitization
        \nPURITY : 93.4%
        \nNUM SUBJECTS : 45
        \nNull: Null (for non-applicable queries to the study)
        \nDILUTIONS: 10% w/w, 15% w/w, 20% w/w
        \nNot applicable. (acceptable response if the query is not applicable to the study. An alternative answer would be Null:Null)
        \n...etc.


        \nYOU MAY START NOW. ADHERE TO THE FORMATTING RULES. Your response should NOT exceed one line. YOU MUST OUTPUT AN ANSWER.
        """
    result = state['llm_client'].invoke(template, node_name="formatter")

    if not result.ok:
        if state['debugging'] == True:
            print(f"formatter LLM call failed ({result.status}): {result.error_message}")
        corrected_output = f"[LLM call failed: {result.status}] {result.error_message or ''}".strip()
    else:
        corrected_output = result.text

    conversation_history = [
        HumanMessage(content=state['final_input']),
        AIMessage(content=output),
        AIMessage(content=corrected_output),
    ]

    return {
        'corrected_output':corrected_output,
        'messages':conversation_history
    }
