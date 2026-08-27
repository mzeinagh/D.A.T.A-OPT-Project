# Django conversion — implementation status

This file exists so "is X actually usable yet?" has one authoritative,
up-to-date answer, separate from the phase-by-phase narrative in commit
messages. Update it whenever a phase's user-facing surface changes.

## Multi-corpus processing (long-report split / integrated-report split)

**Backend: complete. User interface: incomplete — this is a known, tracked
v1 gap, not an oversight.**

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
- `studies.corpus_detection.confirm_detected_corpora()` and
  `process_as_single_corpus()` implement include/exclude, title editing
  (a plain field edit), confirm, and the "process the original PDF as one
  corpus" fallback — fully, and covered by tests
  (`studies/tests/test_corpus_detection.py`).
- **Django admin** (`DetectedCorpusAdmin`, `UploadBatchAdmin`) is wired up
  as a *working* review/confirm surface today — an admin/staff user can
  see every field the eventual page needs and run the "Confirm selected"
  action for real. This is intentionally the interim surface for
  development and administrator use, per the same pattern already used
  for `Question`/`PromptConfig` governance.

What does **not** exist yet:
- A normal, non-admin, authenticated **web page** where a regular lab
  user (not staff) can view their detected corpora, edit titles,
  include/exclude, confirm, fall back to single-corpus processing, or
  cancel. **This is scoped as a required v1 feature of Phase 5, not an
  optional enhancement** — see the Phase 5 entry below.
- Until that page exists, a regular (non-staff) user who uploads a PDF
  that triggers multi-corpus review (a long-report split they requested,
  or an integrated multi-assessment report detected automatically) has
  **no way to complete that upload** — nothing in the ordinary
  application UI can confirm a pending corpus review for them. Only a
  staff user with Django admin access can unblock it today. Don't
  represent multi-corpus processing as usable end-to-end by a regular
  user until Phase 5's review page ships.

## Phase plan (current)

| Phase | Scope | Status |
|---|---|---|
| 0 | Django scaffold, `core_pipeline` port, `llm_client` seam | Done |
| 1 | OpenAI Responses API client (GPT-5) | Done |
| 2 | `studies` data model, admin, prompt-version snapshotting | Done |
| 3 | Celery task, OCR provenance/timeout, usage tracking, cost limit | Done |
| 3.5 | Corpus detection/review generalized (long-report + integrated-report), data/service/admin layer | Done |
| 4 | Upload flow: authenticated upload form + status view (no review/confirm UI) | In progress |
| 5 | Web UI — **including the corpus-review page above as a required v1 feature**, study list/detail, answer views, export views, status polling | Not started |
| 6 | Access & observability hardening, then the legacy-script cutover | Not started |
| 7 | Prompt optimization (deliberately last, after output-parity testing) | Not started |

Phase 4 deliberately does **not** include the corpus-review page — only
the upload form and a status view a user can check after uploading. The
review/confirm page is Phase 5's job, and per the explicit decision
above, it ships as required v1 scope there, not as a "nice to have."
