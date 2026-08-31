# Django conversion — implementation status

This file exists so "is X actually usable yet?" has one authoritative,
up-to-date answer, separate from the phase-by-phase narrative in commit
messages. Update it whenever a phase's user-facing surface changes.

## Multi-corpus processing (long-report split / integrated-report split)

**Backend and user interface: both complete.** This was tracked here as a
known v1 gap through Phase 4; it is closed as of Phase 5.

What exists today:
- `core_pipeline.document_processor.build_corpora()` detects and
  constructs every corpus a PDF yields (single document, long-report
  split via `chunk_report`, or integrated multi-assessment split via
  `chunk_integrated` — toxicity/environmental/residue/other), including
  OCR provenance per page.
- `studies.corpus_detection` persists every detected corpus as a
  `DetectedCorpus` row (title, detection type, assessment category, page
  numbers, shared-vs-specific pages, a preview, and detection warnings),
  pausing before any GPT-5 call for anything beyond the single-corpus,
  no-review-requested case.
- `studies.corpus_detection.confirm_detected_corpora()`,
  `process_as_single_corpus()`, and `cancel_review()` implement
  include/exclude, title editing, confirm, the "process the original PDF
  as one corpus" fallback, and cancel — all lock the `UploadBatch` row
  (`select_for_update()`) for their check-and-transition, so two
  near-simultaneous attempts on the same batch (a double-click, a
  replayed form submission after a refresh) can never both succeed: the
  second one raises `ValueError` cleanly instead of creating a second
  `Study`/`PipelineRun`. Fully covered by tests
  (`studies/tests/test_corpus_detection.py`).
- **A dedicated, non-admin web page** — `studies:review`
  (`studies/views.py::review_view`, template `studies/review.html`) — is
  the primary way any authenticated regular user reviews their own
  uploads: title/category/page ranges/shared-vs-specific pages/preview/
  detection type/warnings are all shown per corpus, with real
  include/exclude checkboxes and an editable title field, and three
  explicit actions (confirm selected / process original PDF as one
  corpus / cancel). Ownership is enforced server-side
  (`_can_access_batch`) — the uploader or any staff user, no one else.
  Stale uploads (already confirmed, cancelled, failed, or still
  detecting) redirect safely to the status page with an explanation
  rather than presenting a form that can't be submitted. Fully covered
  by tests (`studies/tests/test_review_views.py`).
- After confirming, the user is redirected to `studies:upload_status`,
  which now lists every `Study` created from the batch alongside its
  `PipelineRun`(s) and their status/call counts.
- **Django admin** (`DetectedCorpusAdmin`, `UploadBatchAdmin`) remains
  available as a secondary surface for staff/development use (its
  "Confirm selected" action still works), but is no longer the *only*
  way to act on a pending review — see the note in Phase 4's entry below,
  now resolved.

As of Phase 6, the previously-open items above are closed:
- **Failed-detection retry** now has a regular-UI button. A `FAILED`
  `UploadBatch` shows its active error, its full failure history, and a
  "Retry corpus detection" button (owner/staff only) that re-attempts
  detection from scratch, up to `settings.CORPUS_DETECTION_MAX_RETRIES`
  (default 3, env-configurable). Once the limit is reached, retry is
  replaced by a clear "maximum retries reached" message; "Process
  original PDF as one corpus" stays available as the alternative
  regardless of retry count. Duplicate retry submissions are prevented
  the same way confirm/cancel always have been: a synchronous, row-locked
  claim (`corpus_detection.retry_corpus_detection_claim`) happens before
  any Celery task is even enqueued, so a double-click's second request
  fails cleanly rather than starting a second detection attempt.
  `review_view`'s own "process as single" action was also changed to use
  this claim-then-task pattern instead of blocking the request on PDF
  parsing/OCR. Fully covered by tests (`studies/tests/test_retry_flow.py`).
- **General study list/detail, answer, and export views** now exist:
  `studies:study_list` (every study the user can see), `studies:
  study_detail` (one study's runs), `studies:run_detail` (one run's full
  answer transcript, retrieval/call-log detail included), and two
  on-demand export endpoints (`export_run_full` — the complete transcript
  per question; `export_run_answers` — just the formatted answers) that
  stream a `.txt` download rather than writing anything to disk, per
  decision 6 (no permanent transcript files). Ownership enforced the same
  way as every other view. Covered by tests
  (`studies/tests/test_study_views.py`).

## Phase plan (current)

| Phase | Scope | Status |
|---|---|---|
| 0 | Django scaffold, `core_pipeline` port, `llm_client` seam | Done |
| 1 | OpenAI Responses API client (GPT-5) | Done |
| 2 | `studies` data model, admin, prompt-version snapshotting | Done |
| 3 | Celery task, OCR provenance/timeout, usage tracking, cost limit | Done |
| 3.5 | Corpus detection/review generalized (long-report + integrated-report), data/service/admin layer | Done |
| 4 | Upload flow: authenticated upload form + status view | Done |
| 5 | Web UI — dedicated corpus-review page (required v1 feature), status page enhanced to show runs | Done |
| 6 | Failed-detection retry flow, general study/run browsing + export views, observability logging, legacy-cutover documentation | Done |
| 7 | Prompt optimization (deliberately last, after output-parity testing) | Not started |

Phase 6 closed both items Phase 5 had left open (see above) and added:
structured logging (Python's `logging`, not `core_pipeline`'s pre-existing
`print()` debug statements, which are untouched legacy/ported code) across
`studies/tasks.py`, `studies/llm_integration.py`, and `studies/services.py`
— task start/completion/failure, cost-limit halts, and failed LLM calls
are now all visible in the log stream, not just the database. See
`docs/LEGACY_CUTOVER.md` for the legacy CLI pipeline's status: it is
orphaned (nothing in the Django app imports it) but has **not** been
deleted, since no parity testing between it and the Django/GPT-5 pipeline
has been possible in this environment (`api.openai.com`/`huggingface.co`
are both blocked by the sandbox's egress proxy) — that document lists
what a parity pass would need before deletion is safe to decide on.
