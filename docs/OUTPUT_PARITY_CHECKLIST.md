# Output-parity test checklist

Companion to `docs/END_TO_END_TESTING_GUIDE.md` — use this once you've
processed a study through the Django app (§8 of that guide) and want to
compare it against the legacy CLI pipeline for the same input PDF. This
is the artifact Phase 7 (prompt optimization) is gated on — see §11 at
the end for exactly what to hand back before that phase starts.

"Parity" doesn't mean byte-identical output. The Django pipeline runs a
different model (GPT-5 via the Responses API, vs. the legacy pipeline's
local Ollama `phi4`) and includes three bug fixes only applied to the
ported `core_pipeline/` copy — see `docs/LEGACY_CUTOVER.md` for exactly
which three. What matters is: same retrieved pages, same or better
extracted facts, no silent divergence in corpus splitting/section
classification, and answer quality you'd actually trust more (or at least
not less) than the legacy output.

## 10a. Running the legacy pipeline for the same PDF

The legacy pipeline (`main.py`) is a batch script, not a web app — it
scans an entire folder (`pdf_dir`) and processes every PDF it finds. To
run it against just your one test PDF:

1. **Put a local Ollama server up with the model `config.py` expects**:
   ```bash
   ollama pull phi4
   ollama serve   # if not already running as a service
   ```
   This is entirely separate infrastructure from the Django app's GPT-5
   config — no `OPENAI_API_KEY`/Postgres/Redis/Celery involved for this
   half of the comparison.

2. **Point `config.py` at your test PDF.** It's currently hardcoded to a
   Windows path (`chats_dir`/`pdf_dir` both point at
   `C:\Users\Grace\Documents\...`) that won't exist on your machine —
   edit those two lines locally to a folder containing *only* your test
   PDF, and an output folder for the results. This is a local edit for
   your own test run, not a repo change — **don't commit it** (or commit
   it on a throwaway local branch you don't push, if you want it under
   version control for your own reference).

3. **Run it:**
   ```bash
   python3 main.py
   ```
   `chats_dir` gets two new files per PDF once it finishes:
   `<pdf_name>_run_1_full_convo.txt` (the complete conversation — guide
   retrieval, augmented prompt, and raw model response per question) and
   `<pdf_name>_run_1_response_only.txt` (just the responses), plus a
   trailing line reporting total processing time in minutes. These are
   the legacy-side inputs for the table below — directly analogous to the
   Django app's own **Download full conversation** / **Download answers
   only** exports (§8.6 of the testing guide).

Known, expected differences going in: the legacy run has no token/cost
tracking at all (local model, no per-call API cost) — leave those columns
below marked `N/A (legacy)` rather than `0`, which would misleadingly
suggest it processed for free by design comparison rather than by not
tracking it.

## 10b. Per-question comparison table

Copy this table once per `Study`/PDF you test. Fill one row per question
(11 rows for the standard question set from §6 of the testing guide).

| # | Question (short) | Legacy answer | Django/GPT-5 answer | Reference/expected answer | Source pages (legacy) | Source pages (Django) | Correct? | Complete? | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Exposure type/method | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 2 | Purity | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 3 | Vehicle/solvent | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 4 | Test guideline followed | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 5 | *(fill from Question table)* | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 6 | | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 7 | | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 8 | | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 9 | | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 10 | | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |
| 11 | | | | | | | ☐ Y ☐ N | ☐ Y ☐ N | |

**Column definitions:**
- **Legacy / Django/GPT-5 answer**: the formatted answer text from each
  pipeline's output (the "Formatted answer" in the Django run detail
  page / export; the response text in the legacy `*_response_only.txt`).
- **Reference/expected answer**: what a human reviewer familiar with the
  source study would consider correct — fill this in independently of
  either pipeline's output, ideally before looking at either, to avoid
  anchoring.
- **Source pages**: which PDF page(s) each pipeline actually retrieved
  and used to answer (Django: the run detail page shows retrieved page
  references per answer; legacy: check the `*_full_convo.txt` file for
  the retrieved context block).
- **Correct?**: does the answer match the reference answer's substance
  (not necessarily its exact wording)?
- **Complete?**: does it address every part of the question (e.g.
  question 1 asks for both exposure *type* and exposure *method* — a
  "correct but partial" answer should be marked incomplete here, with a
  note).

## 10c. Per-study summary

One of these per PDF/`Study` tested, in addition to the per-question
table above.

| Metric | Legacy | Django/GPT-5 |
|---|---|---|
| Total processing time | *(from legacy's trailing log line, minutes)* | *(PipelineRun.started_at → finished_at, or the run detail page's timestamps)* |
| Total input tokens | N/A (legacy) | *(PipelineRun.total_input_tokens, run detail page)* |
| Total output tokens | N/A (legacy) | *(PipelineRun.total_output_tokens)* |
| Total LLM calls | *(count manually from full_convo.txt — up to 3/question: retrieve_guide, generate, formatter)* | *(PipelineRun.total_api_calls)* |
| Estimated cost (USD) | N/A (legacy) | *(PipelineRun.estimated_cost_usd)* |
| Errors/warnings encountered | *(anything printed to console, or a question silently skipped)* | *(Study.error_message, PipelineRun.error_message, failed LLMCallLog rows, StudyPage.ocr_error — see testing guide §9)* |
| Corpus detection result | *(did the legacy split behave as expected? see notes)* | *(DetectedCorpus rows — detection_type, assessment_category, warnings)* |
| Overall verdict | ☐ Legacy better ☐ Django better ☐ Equivalent | |

## 11. What I need from you before Phase 7

Phase 7 is prompt optimization — changing `intro`/`few_shots`/individual
question text based on real output quality. I can't respons­ibly start
that without seeing what real GPT-5 output actually looks like against
real studies, since I have no way to run it myself here. Please bring
back:

1. **At least 2–3 completed §10b/§10c tables** — ideally covering a
   single-assessment PDF, a long-report-split PDF, and an
   integrated-report (multi-assessment) PDF if you have examples of each,
   since those exercise different `core_pipeline` code paths.
2. **Any answer you marked incorrect or incomplete**, with what you'd
   have expected instead — this is the actual raw material Phase 7 needs;
   without it, "optimize the prompts" has nothing concrete to aim at.
3. **Any error/warning you hit** (from testing-guide §9) that isn't
   already covered by the retry/fallback UI — a genuine bug report, not
   a prompt issue, and something I'd fix separately from Phase 7.
4. **Real cost/token numbers** from a handful of runs, so
   `OPENAI_COST_LIMIT_USD` can eventually be set to something evidence-
   based rather than guessed (see the note in testing-guide §2 about why
   `OPENAI_COST_LIMIT_ENABLED` should stay off for now).
5. **Your overall verdict** on whether GPT-5's output is trustworthy
   enough, as-is, to be worth optimizing — if it's badly off in some
   structural way (not just wording), that's worth a conversation before
   Phase 7 starts tuning prompts around it.
