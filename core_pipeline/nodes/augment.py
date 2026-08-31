"""Builds the final LLM prompt from the retrieved context, guide, and question.

Ported unchanged from the original DATA_Project pipeline — pure string
templating, no LLM call, so the GPT-5 migration doesn't touch it. Preserving
this exactly is part of "preserve existing prompts and behavior as closely
as possible" for the initial migration.
"""
from ..state import GraphState


def augment(state:GraphState):

    if state['debugging'] == True:
        print("Augmenting...")

    txts = state['context']
    intro = state['intro']
    few_shots = state['few_shots']
    question = state['question']
    guide = state['guide']

    if guide is not None:
        if len(state['context']) > 0:
            texts = '\n\n'.join(txts)
            clean_texts = texts

            input_text = f"""
                {intro}
                \n{question}
                \nYou should use the following guide to retrieve your information:
                \n{guide}
                \n{few_shots}
                \nStudy Report (raw text):
                \nIf a section is irrelevant, nonsensical, or does not help answer the question, ignore it.
                \n--------------------------------------------------------------------------BEGIN EXERPT--------------------------------------------------------------------------
                \n{clean_texts}
                \n--------------------------------------------------------------------------END EXERPT--------------------------------------------------------------------------
                \nYOU MAY NOW WRITE YOUR ANSWER, STOP GENERATING after you've answered the question, you MUST output an answer.
            """
        else:
            input_text = f"""
                Question:\n{state['question']}
                \nIMPORTANT: There is no information found on the toxicology report that may provide an answer to the question. This question has no answer.
                """

    elif guide is None:
        if len(state['context']) > 0:
            texts = '\n\n'.join(txts)
            clean_texts = texts

            input_text = f"""
                \n{intro}
                \n{question}
                \n{few_shots}
                \nStudy Report (raw text):
                \nIf a section is irrelevant, nonsensical, or does not help answer the question, ignore it.
                \n--------------------------------------------------------------------------BEGIN EXERPT--------------------------------------------------------------------------
                \n{clean_texts}
                \n--------------------------------------------------------------------------END EXERPT--------------------------------------------------------------------------
                \nYOU MAY NOW WRITE YOUR ANSWER, STOP GENERATING after you've answered the question, you MUST output an answer.
            """
        else:
            input_text = f"""
                Question:\n{state['question']}
                \nIMPORTANT: There is no information found on the toxicology report that may provide an answer to the question. This question has no answer.
                """

    return{'augmented_question':input_text}
