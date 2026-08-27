"""The fixed question set (Q1...Q11) asked against every study, plus the
shared intro/few-shot framing prepended to each prompt.

Ported unchanged from the original DATA_Project pipeline. This module is
the seed source for the `studies.Question` / `studies.PromptConfig` models
(Phase 2) — once those exist, the database becomes the source of truth and
admins edit questions there instead of here. Until that migration lands,
`core_pipeline` callers that don't yet have a Django-backed question set can
still import `inputs`/`intro`/`few_shots` directly from this module.
"""

# input format example ['question 1',['what is...?' -> query,['micronucles','in vivo' -> keywords]]]
inputs = [
    ['question 1', [
        """
            Determine the exposure type (ORAL, DERMAL, or INHALATION) for the toxicology study, then classify the exposure method based on these rules:\n
            -DERMAL study exposure methods: Topical Application, Intradermal Injection, or Occlusive Patch \n
            -ORAL study exposure methods: gavage or feed  \n
            -INHALATION study exposure methods: Powder, Vapor, or Gas Chamber\n
            THIS QUESTION DOES NOT APPLY TO IN VITRO STUDIES! (non-applicable)
    """, []
    ]],
    ['question 2', [
        """
        Find the purity of the tested substance for this toxicology report.
        """, ['purity']]
    ],
    ['question 3', [
        """
        Find the vehicle(s) or solvent(s) used in this study to dissolve the test substance.\n
        Look for pages containing text like "w/v", "%", "v/v", they usually tell you the vehicle used for dissolving the test substance.\n
        Examples include: alcohol, water, methanol, DMSO, oils, aqueous methylcellulose, acetone, petrolatum, gelatin capsule — but other answers are allowed.
        """, []]],
    ['question 4', [
        """
        Test guidelines can help legitimize studies. Guidelines are often from OECD, ECC or EC. For example, OECD 471 is a type of guideline. Does this study follow a test guideline?
        """, ['OECD', 'ECC', 'EC']
    ]],
    ['question 5', [
        """
        Test methods are well known methods that say what kind of study is being performed. Guineau pig maximisation,
        \nAmes test, Micronucleus test, Human Repeat Insult Patch test, Guineapig Maximization test are examples of test methods. Does this study follow a test method?
        """, []
    ]],
    ['question 6', [
        """
        What was the maximum dosage of the test substance used on the test subject, with unit? Please note that doses can have various units, such as % (percentage), mg/kg, mg/plate ...etc.\nDo NOT report the dose used in the solubility study, only report the highest dose used on the test subject.
        """, []
    ]],
    ['question 7', [
        """
        Is the substance ever diluted?\n Look for symbols like "w/v", "%", "v/v" ...etc.\n If yes, state the dilution percentage or percentages. If no, answer "null". You do NOT need to state the solvent, only the dilution percentages or 'null'.
        """, []
    ]],
    ['question 8', [
        """
        What is the total number of animals used in the study? If it is not mentioned, answer "null".
        """, []
    ]],
    ['question 9', [
        """
        Answer this question only if the study is a *repeated dose or sensitization* study, otherwise, answer 'not applicable'.\n
        Find the Hazard Classification of this study.\n
        Was the substance classified as low, low-moderate, moderate, moderate-high, high, or extreme hazard? If there is no info, answer "null".\n
        If there is NO mention of NOAEL, negative effect level, NOEL, NOEC, or anything of the like in the study, report 'not applicable'.\n
        If the toxicity endpoint of the study was reported to be negative or inconclusive, report 'not applicable'.\n
        """, []
    ]],
    ['question 10', [
        """
        Answer this question only if the study is a *repeated dose* study. Otherwise, answer 'not applicable'. \n
        What was the TESTING duration of the study (not the dates during which the study is conducted), including units (days, weeks, years). If it's not mentioned, answer "null".\n
        Do NOT explain the methodology or categorize the study into 'Full study' or 'Summary'; ONLY state the duration of the study IF it is a *repeat dose* study.\n
        """, []
    ]],
    ['question 11', [
        """
        Answer this question only if the study is a *repeated dose* study. Otherwise, answer 'not applicable'. \n
        What Critical Effects on the test animals changed the NOAEL or the classification hazard? This could be changes in food consumption, organ weight change, weight change, or any other health problem observed in the animal due to the substance. If there were none, answer "null".\n
        Do NOT respond with the hazard classification, only respond if there were Critical Effects that CHANGED the OUTCOME of the study.
        """, []
    ]]]

intro = "You are a chemical toxicity evaluator. Your job is to read a toxicity report and retrieve specific information from the report."
few_shots = "Format your answer as: <CATEGORY>: <ANSWER>. \n If either is missing or unclear, return Null. \n Examples of acceptable answers:\n DERMAL: Topical Application  \n ORAL: gavage  \n Null : Null \n PURITY: 92% \n MAX DOSE: 50% w/w \n ...etc."
