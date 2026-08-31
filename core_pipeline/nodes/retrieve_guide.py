"""Finds the most relevant section of the student handbook for the current question.

Ported from the original DATA_Project pipeline. The only change: the local
Ollama call (`from config import llm`, then `a.pretty_repr()` on its
LangChain return object) is replaced by `state['llm_client'].invoke(...)`
and its plain-string `.text`. The title-matching/negation logic below is
otherwise untouched — it operates on the same text either way, since
`pretty_repr()` only ever added a LangChain header around the same content.
"""
import re

import pymupdf

from ..state import GraphState


def retrieve_guide(state:GraphState):
    if state['debugging'] == True:
        print("Retrieving Guide...")

    query = state['question']
    guidebook_fp = state['guidebook_fp']
    llm_client = state['llm_client']

    relevent_page_content = None
    relevent_title = None

    with pymupdf.open(guidebook_fp) as doc:
        pages = [page.get_text() for page in doc]
        titles = []
        for page in pages:
            match_title = re.search(r'\+\+\+([^+]+)\+\+\+', page)

            if match_title:
                titles.append(match_title.group(1).strip())

        # Pulled out of the f-string below: an f-string expression can't
        # contain a backslash on Python < 3.12 (PEP 701), and this project
        # targets 3.10+. Output is byte-identical either way.
        titles_block = "\n".join(titles)

        q = f"""
            Here are a list of titles from a manual containing information on how to identify the answers to a certain user's query. Pick the section from an evaluator's manual that best describes
            the category of the query. The section should handle and explain how to solve the user's query almost perfectly; if no sections feel right, simply output "Non".\n
            If the query asks for the 'test method', output 'Non'!!\n
            Sections titles:\n{titles_block}\n
            User's Query: {query} \n
            You should only choose ONE or NONE title(s) that best describes the query. Output only the best-match Title or 'Non', no other text.\n
            Examples of acceptable outputs: "Vehicle / Solvent", "Test Item Concentration / Dilution" ...etc.\n
            Examples of unacceptable outputs: "The best titles that match the query are 'vehicles' and 'dilution' and 'sample size'" ...etc.\n
            If no titles are tightly relevant to the query, output "Non"! No other text is allowed; you do not need to explain your reasoning. \n
            Remember, If the query asks for the 'test method', output 'Non'.\n
            You may begin now.
            """

        result = llm_client.invoke(q, node_name="retrieve_guide")
        if not result.ok:
            if state['debugging'] == True:
                print(f"retrieve_guide LLM call failed ({result.status}): {result.error_message}")
            return {'guide': None}

        answer_text = result.text

        # Count the last occuring matching title; In the event that the model begins to explain its choice, the last matching title should almost certainly be the answer.
        number_of_occurances = {}
        for title in titles:
            if answer_text.lower().strip().count(title.lower()) >= 1:
                number_of_occurances[title] = answer_text.count(title)

        # Count 'Non' - add 'non' as a title to number_of_occurances if non exists in answer
        non_match = r"\bnon\b"
        if len(re.findall(non_match,answer_text.lower().strip())) >= 1:
            number_of_occurances['non'] = answer_text.lower().strip().count('non')

        if len(number_of_occurances) > 1: # More than 1 titles appeared in the response
            last_indexes = {}
            for title, value in number_of_occurances.items():
                last_index = answer_text.rfind(title) # Case Sensitive!
                last_indexes[title] = last_index

            if 'non' in number_of_occurances.keys():
                non_idxs = [ m.start() for m in re.finditer(non_match, answer_text.lower().strip()) ]
                if len(non_idxs) > 0:
                    last_non_idx = non_idxs[-1]
                    last_indexes['non'] = last_non_idx

            last_title = max(last_indexes, key=last_indexes.get) # last occuring title

            # Check if last occuring title is a "negative case" or not. (i.e. " 'Title' is not applicable for this case.")
            def is_negative(last_title:str, window_front_cut: int, window_end_cut:int, response:str):
                negative_pattern = fr"""
                    (?:["']?{re.escape(last_title)}["']?(?:\s+\w+){{0,2}}\s+\b(?:no|not|wrong)\b)
                    |
                    (?:\bnot\b\s+["']?{re.escape(last_title)}["']?)
                """
                if window_front_cut and window_end_cut:
                    string_to_search = response[window_front_cut:window_end_cut]

                elif (window_front_cut) and not (window_end_cut):
                    string_to_search = response[window_front_cut:]

                else:
                    string_to_search = response

                return bool(re.search(
                        negative_pattern,
                        string_to_search,
                        flags=re.IGNORECASE | re.VERBOSE
                    ))

            # Now check
            filtered_last_indexes = last_indexes.copy()
            is_last_title_negative = is_negative(last_title, filtered_last_indexes[last_title] - 50, None, answer_text.lower().strip())

            while is_last_title_negative:
                del filtered_last_indexes[last_title]

                if not filtered_last_indexes: # no more items left in list
                    last_title = None
                    break

                last_title = max(filtered_last_indexes, key=filtered_last_indexes.get)

                is_last_title_negative = is_negative( last_title, filtered_last_indexes[last_title] - 50, None, answer_text.lower().strip() )

            relevent_title = last_title

            #if relevent_title == 'non': # end early if last title is non
            #    return {'guide':None}

            for p in pages:
                if f"+++{relevent_title}+++" in p:
                    relevent_page_content = p

        else: # only 1 title in response
            relevent_title = next(iter(number_of_occurances),None)
            for p in pages:
                if f"+++{relevent_title}+++" in p:
                    relevent_page_content = p

    return {'guide':relevent_page_content}
