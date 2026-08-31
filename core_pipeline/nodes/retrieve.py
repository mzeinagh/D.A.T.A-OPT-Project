"""Searches the VectorStore for relevant pages and assembles the retrieval context.

Ported unchanged from the original DATA_Project pipeline — this node never
called the LLM, so the GPT-5 migration doesn't touch it.
"""
from ..state import GraphState


def retrieve(state: GraphState):
    if state['debugging'] == True:
        print("Retrieving Context...")

    query = state['question']
    store = state['corpus_store']

    retrieved_pages = {} # for debugging!

    keywords = state['keywords']

    if keywords is not None:
        store.keywords = keywords # update store

    # use store to perform search
    rerank_results = store.search(query=query)
    context = []
    page_numbers = []

    # Append relevant sections to the context
    summary = state['summary']
    title_page = state['title_page']

    if title_page is not None:
        context.append(title_page.content)
        page_numbers.append(title_page.metadata['page_num'])

    if summary is not None:
        context.append(summary.content)
        page_numbers.append(summary.metadata['page_num'])

    # Begin adding retrieval results to context
    for c in rerank_results:
        page_text = c.document.content
        page_number = c.document.metadata["page_num"]

        context.append(page_text)
        page_numbers.append(page_number)

    retrieved_pages["length"] = int(len(context))
    retrieved_pages["page numbers"] = page_numbers

    return {
        'context':context,
        'retrieved_pages': retrieved_pages
    }
