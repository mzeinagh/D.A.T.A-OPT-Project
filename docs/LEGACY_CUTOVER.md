# Legacy CLI pipeline — cutover status

This documents the status of the **original CLI pipeline files** (the
pre-Django code this whole project was converted from), per the Phase 6
scope item "legacy cutover: document status, do NOT delete without parity
testing." Nothing described here has been deleted or modified.

## What "legacy" means here

The root-level files that predate the Django conversion:

```
main.py  config.py  document_processor.py  utils.py  models.py
state.py  graph.py  questions.py  run_methods.py
nodes/       (augment.py, formatter.py, generate.py, retrieve.py, retrieve_guide.py)
search/      (bm25.py, vector_store.py)
```

These are **not** the same files the Django app runs. Phase 0 ported a
copy of this logic into `core_pipeline/` (plus `llm/` for the GPT-5
client), adapting it to Django (settings-driven config, an injected
`llm_client` instead of a hardcoded local Ollama call, etc.) and fixing
three real bugs found along the way — see "Known divergences" below.

## Current status: orphaned, not deleted

A dependency check (`grep` for imports of every legacy module across
`core_pipeline/`, `llm/`, `studies/`, and `dataopt/`) finds **zero
references** — nothing in the Django app imports `main.py`, `config.py`,
`document_processor.py`, `utils.py`, `models.py`, `state.py`, `graph.py`,
`questions.py`, `run_methods.py`, `nodes/`, or `search/` from the repo
root. The app is fully self-contained inside `core_pipeline/`/`llm/`/
`studies/`. These legacy files are dead code from the running
application's point of view today.

They have **not been deleted**, for one reason: **no parity testing has
been possible in this environment.** `api.openai.com` and
`huggingface.co` are both blocked by this sandbox's egress proxy
(confirmed via direct `curl` — `CONNECT tunnel failed, response 403`), so
there has been no way to run the legacy CLI pipeline and the new Django/
GPT-5 pipeline against the same input PDF and diff their output. Deleting
the legacy files before that comparison exists would remove the only
reference implementation available to check the port against, with no way
to reconstruct it from git history alone being materially different from
just... not deleting it yet.

## Known divergences between legacy and `core_pipeline/`

Three bugs were found in the legacy files during the port. Per an
explicit standing instruction, **none of them were fixed in the legacy
files themselves** — only in the `core_pipeline/` copies the Django app
actually runs. This means the legacy pipeline, if run as-is today, still
exhibits these bugs:

1. **`nodes/retrieve_guide.py`** — an f-string embeds
   `{"\n".join(titles)}` directly inside its expression, which is a
   `SyntaxError` on Python versions before 3.12 (PEP 701). Fixed in
   `core_pipeline/nodes/retrieve_guide.py` by extracting
   `titles_block = "\n".join(titles)` before the f-string.
2. **`document_processor.py`'s `_find_title_page`** — compares
   `title_page_likeliness(text)`'s return value (a `(probability,
   features)` tuple per its own docstring) directly against `0.80`,
   raising `TypeError` on any real call. Fixed in
   `core_pipeline/document_processor.py` by unpacking
   `probability, _features = title_page_likeliness(text)` first.
3. **`document_processor.py`'s corpus-building helpers** — used
   `target_high_level = target_high_level or [...]` /
   `negative_titles = negative_titles or [...]`, which silently replaces
   an intentionally-passed empty list (`[]`, meant to mean "no
   restriction") with the hardcoded default, because Python's `or`
   treats `[]` as falsy. Fixed in `core_pipeline/document_processor.py`
   with explicit `is None` checks. This one matters beyond a crash: it's
   the mechanism `studies/corpus_detection.py` relies on to surface
   environmental/residue assessment sections during detection instead of
   silently filtering them out before they can ever be classified.

Beyond these three fixes, `core_pipeline/` also gained OCR page
provenance/timeout tracking, multi-corpus detection metadata
(`source`/`page_numbers`/`shared_page_numbers`/`assessment_category`/
`detection_warnings`), and the GPT-5 client seam (`llm/`) replacing the
hardcoded local Ollama call — none of which exist in the legacy files.
So "parity" here doesn't mean byte-identical output; it means: for the
same input PDF, do the two pipelines retrieve the same passages and reach
materially the same answers, modulo the model swap (GPT-5 vs. the local
model) and the three bug fixes above.

## What would need to happen before deletion

1. **Real API access** — either run this outside the current sandbox's
   network restrictions, or have a human run the comparison locally/in
   CI with real `OPENAI_API_KEY` and a downloaded `sentence-transformers`
   model.
2. **A parity run**: the same input PDF(s) through both the legacy CLI
   pipeline (`main.py`) and the Django app's `run_pipeline_task`,
   comparing retrieved pages/passages and reviewing answer quality
   side-by-side. Exact-text equality isn't the bar (different LLM,
   the three bug fixes) — no silent divergence in retrieval or
   corpus-splitting behavior is.
3. **An explicit decision to delete**, made by a person after reviewing
   that comparison — not inferred from "the app doesn't import it
   anymore."

Until then, the legacy files stay in the repo, unmodified, as the
reference implementation this project was converted from.
