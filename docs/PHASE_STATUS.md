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

What does **not** exist yet (unrelated to the corpus-review gap above,
just not yet built):
- A general study list/detail view, answer views, or export views —
  `studies:upload_status` is the only per-batch overview page so far.
- Any admin-triggered retry/fallback action surfaced in the *regular*
  UI for a batch already marked `FAILED` before Phase 5 landed — the
  review page's fallback actions only appear while a batch is
  `AWAITING_CONFIRMATION`. A `FAILED` batch's status page links to the
  review page, but `review_view` itself only renders the form for a
  batch actually awaiting confirmation; a failed batch needs a fresh
  detection attempt (currently only triggerable via `manage.py shell`/
  Django admin, not a button in the regular UI). Worth a small follow-up
  if failed detections turn out to be common in practice.

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
| 6 | Access & observability hardening, general study list/detail + answer/export views, then the legacy-script cutover | Not started |
| 7 | Prompt optimization (deliberately last, after output-parity testing) | Not started |

Phase 5 delivered specifically the corpus-review page and the retry/
fallback/cancel actions around it. A broader study list/detail and
answer/export browsing experience is still open — folded into Phase 6
rather than tracked as a separate phase, since it's naturally paired with
the access-control hardening pass.
