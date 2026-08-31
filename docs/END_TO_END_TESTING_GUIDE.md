# End-to-end testing guide

For running the Django application (branch `django-app`, commit `4256b34`
and later) on a machine with real network access — this sandbox cannot
reach `api.openai.com` or `huggingface.co`, so none of this has been
exercised against the real GPT-5 API or a downloaded embedding model. This
guide gets you from a clean checkout to a processed study so you can judge
that for yourself, and feeds directly into
`docs/OUTPUT_PARITY_CHECKLIST.md`.

Nothing in this guide changes prompts, pipeline behavior, or production
code — see `docs/PHASE_STATUS.md`/`docs/LEGACY_CUTOVER.md` for what's
actually shipped.

## 0. Prerequisites

- Python 3.11+ (what this project has been developed/tested against)
- PostgreSQL 14+ and Redis, installed and controllable locally (see §7 for
  start commands — exact service names vary by OS/package manager)
- `pip install -r requirements.txt` from the repo root
- A real `OPENAI_API_KEY` with access to whatever model you set
  `OPENAI_MODEL` to (the client is provider-agnostic in code, but is only
  built for the OpenAI Responses API today)
- Network access to `huggingface.co` (to download the embedding model,
  §4) and to Docling's own model downloads (§0a)
- The test PDF(s) you intend to run through both pipelines

### 0a. Docling's own first-run download

`core_pipeline`'s OCR fallback (`ocr_docling`, wrapping
`docling.document_converter.DocumentConverter`) downloads its own
layout/OCR models from Hugging Face the first time it actually runs OCR
on a page — separately from, and in addition to, the sentence-transformers
embedding model in §4. This only happens for pages that trigger OCR (a
page whose direct text extraction fails or looks unreliable), not for
every upload. Expect the first OCR-triggering upload to pause noticeably
longer than later ones while Docling caches its models (typically to
`~/.cache/huggingface` or `~/.cache/docling`, depending on Docling
version) — this is normal, not a hang.

## 1. Starting point

Confirmed for this guide: branch `django-app`, HEAD at commit `4256b34`
("Phase 6: observability logging + legacy-cutover documentation"),
working tree clean. `git log --oneline -1` on your checkout should show
that commit (or a later one on the same branch, if you've since pulled
further work).

## 2. Environment variables

Copy `.env.example` to `.env` and fill in every value below (placeholders
only — never commit `.env`, it's gitignored). `manage.py`/`celery` don't
auto-load `.env` themselves — export these into your shell first (e.g.
`set -a && source .env && set +a` on bash/zsh) before running any command
in §7.

| Variable | Purpose | Example / placeholder |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | Which settings module to use | `dataopt.settings.dev` |
| `DJANGO_SECRET_KEY` | Django's secret key | `<generate one — see below>` |
| `DJANGO_DEBUG` | Show full tracebacks on error | `true` |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated allowed Host headers | `localhost,127.0.0.1` |
| `POSTGRES_DB` | Database name | `data_opt` |
| `POSTGRES_USER` | Database role | `data_opt` |
| `POSTGRES_PASSWORD` | Database role's password | `<your local password>` |
| `POSTGRES_HOST` | Database host | `localhost` |
| `POSTGRES_PORT` | Database port | `5432` |
| `CELERY_BROKER_URL` | Redis URL, broker role | `redis://localhost:6379/0` |
| `CELERY_RESULT_BACKEND` | Redis URL, result backend role | `redis://localhost:6379/0` |
| `EMBEDDING_MODEL_PATH` | Local path to the embedding model (§4) | `/absolute/path/to/embeddings_local/all-MiniLM-L6-v2` |
| `HANDBOOK_PDF_PATH` | Path to the evaluator's handbook PDF | `/absolute/path/to/dependants/Structured EAU1 _student_ handbook (2).pdf` |
| `OCR_TIMEOUT_SECONDS` | Per-page OCR timeout | `60` |
| `CORPUS_DETECTION_MAX_RETRIES` | Max failed-detection retries before the UI stops offering "Retry" | `3` |
| `OPENAI_API_KEY` | Your real API key | `<your key — never share or commit>` |
| `OPENAI_MODEL` | Model name for the Responses API | `gpt-5` (or whatever you intend to test) |
| `OPENAI_MAX_OUTPUT_TOKENS` | Per-call output token cap | `1024` |
| `OPENAI_TIMEOUT_SECONDS` | Per-call timeout | `60` |
| `OPENAI_MAX_RETRIES` | Per-call retry count on transient failure | `3` |
| `OPENAI_COST_LIMIT_ENABLED` | Per-run cost ceiling on/off | `false` (leave off for the first test runs — see note) |
| `OPENAI_COST_LIMIT_USD` | The ceiling itself, if enabled | *(blank unless enabled)* |

**Generating `DJANGO_SECRET_KEY`:** `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`

**Note on `OPENAI_COST_LIMIT_ENABLED`:** this is off by default because no
real benchmark data exists yet to set a sensible ceiling — your parity
runs are exactly what would generate that data. Leave it disabled for the
first several runs so a run never gets cut short mid-question; once you
have real per-run cost numbers (§10's checklist captures them), you can
decide on a sensible limit for routine use.

## 3. Database setup

```bash
# create the role and database once (adjust to your local Postgres setup)
sudo -u postgres psql -c "CREATE ROLE data_opt WITH LOGIN PASSWORD '<your password>' CREATEDB;"
sudo -u postgres psql -c "CREATE DATABASE data_opt OWNER data_opt;"

# apply migrations
python3 manage.py migrate
```

Confirm no pending model changes: `python3 manage.py makemigrations --check --dry-run` should print "No changes detected".

## 4. The local embedding model

Per decision 3 (embeddings stay local, no OpenAI embedding API, no model
weights committed to git), `embeddings_local/all-MiniLM-L6-v2/` in the
repo only contains a placeholder file (`hi.txt`) explaining where to get
the real model. Two ways to point the app at a real model:

**Option A — download the real files into that folder** (keeps
`EMBEDDING_MODEL_PATH` pointed at the repo's own directory, the default in
`.env.example`):

```bash
pip install huggingface_hub
python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('sentence-transformers/all-MiniLM-L6-v2', local_dir='embeddings_local/all-MiniLM-L6-v2')
"
```

**Option B — let `sentence-transformers` manage its own cache**, and point
`EMBEDDING_MODEL_PATH` at the model name instead of a local path:

```
EMBEDDING_MODEL_PATH=sentence-transformers/all-MiniLM-L6-v2
```

`core_pipeline/search/vector_store.py` just passes `EMBEDDING_MODEL_PATH`
straight to `SentenceTransformer(...)`, which accepts either a local
directory or a Hugging Face model id — either option works with zero code
changes. Option B downloads to `~/.cache/torch/sentence_transformers` (or
similar) on first use rather than into the repo.

Either way, confirm it loads before your first real test run:

```bash
python3 -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'dataopt.settings.dev')
django.setup()
from django.conf import settings
from sentence_transformers import SentenceTransformer
m = SentenceTransformer(settings.EMBEDDING_MODEL_PATH)
print('OK, embedding dim:', m.get_sentence_embedding_dimension())
"
```

## 5. Creating the initial administrator account

```bash
python3 manage.py createsuperuser
```

Follow the prompts (username, email, password). This account gets both
`is_staff` and `is_superuser` — it can log into `/admin/` and, in the
regular app, sees and can act on *every* user's uploads/studies (staff
access is intentionally broader than an ordinary account — see
`_can_access_batch`/`_can_access_study` in `studies/views.py`).

For a second, ordinary (non-staff) account to test the "owner-only" access
paths, either register a normal user via Django admin
(`/admin/auth/user/add/`, leaving "Staff status" unchecked) or:

```bash
python3 manage.py shell -c "
from django.contrib.auth import get_user_model
get_user_model().objects.create_user('labuser', password='<a password>')
"
```

## 6. Seeding the 11 questions and the active prompt configuration

There is no seed *command* — Question/PromptConfig governance is
deliberately Django-admin-only (decision 9), and manually re-typing 11
question texts through the admin UI isn't practical for a first test run.
The exact same text `core_pipeline/questions.py` already carries (the
ported, currently-unedited question set and intro/few-shots framing) can
be loaded with one `manage.py shell` script — this does not create a new
management command or change any file, it just populates rows from
content that's already in the repo:

```bash
python3 manage.py shell <<'PYEOF'
from studies.models import Question, PromptConfig
from core_pipeline import questions as legacy_questions

if not Question.objects.exists():
    for order, (label, (text, keywords)) in enumerate(legacy_questions.inputs):
        Question.objects.create(text=text.strip(), keywords=keywords, order=order, active=True)
    print(f"Created {Question.objects.count()} questions.")
else:
    print(f"Question table already has {Question.objects.count()} row(s) — skipped.")

if not PromptConfig.objects.filter(name="default").exists():
    config = PromptConfig.objects.create(
        name="default",
        intro_text=legacy_questions.intro,
        few_shots_text=legacy_questions.few_shots,
    )
    config.activate()
    print("Created and activated 'default' PromptConfig.")
else:
    print("PromptConfig 'default' already exists — activate it manually in /admin/ if needed.")
PYEOF
```

Verify in `/admin/studies/question/` (11 rows, all active) and
`/admin/studies/promptconfig/` (one row, "active" shown). A `PipelineRun`
cannot start without exactly one active `PromptConfig` —
`services.get_or_create_current_prompt_config_version` raises
`NoActivePromptConfigError` otherwise, which surfaces as a failed Celery
task (§9).

If you want to test with a *different* question set or prompt framing
before Phase 7, edit rows directly in `/admin/` — nothing about this
guide constrains you to the legacy text verbatim; it's just a known-good
starting point that matches what the legacy pipeline itself asks, which
is what makes an apples-to-apples parity comparison possible.

## 7. Starting the four services

Four long-running processes, each in its own terminal, from the repo
root, with the `.env` variables exported into each shell first:

```bash
# 1. PostgreSQL — start however your OS/package manager normally does.
#    Common examples (use whichever matches your machine):
sudo service postgresql start        # Debian/Ubuntu
sudo systemctl start postgresql      # systemd-based distros
brew services start postgresql@16    # macOS (Homebrew)

# 2. Redis
redis-server --daemonize yes --port 6379
# or: sudo service redis-server start / brew services start redis

# 3. Django dev server
python3 manage.py runserver

# 4. Celery worker — this is not optional. Nothing happens after upload
#    (detection stays "Detecting" forever, confirmed corpora never start
#    processing) without a worker consuming the queue. --loglevel=info
#    matters for §9 below — see the note there.
celery -A dataopt worker --loglevel=info
```

Confirm each is actually up before uploading anything:

```bash
pg_isready -h localhost -p 5432
redis-cli -h localhost -p 6379 ping        # expect: PONG
curl -sf http://localhost:8000/accounts/login/ >/dev/null && echo "Django OK"
# the celery worker terminal itself prints "celery@<host> ready." on startup
```

## 8. Uploading one PDF and following it through

1. Log in at `http://localhost:8000/accounts/login/`.
2. Go to `http://localhost:8000/studies/upload/` (or the "Upload" link in
   the header). Choose your test PDF. Leave "Split this PDF into multiple
   studies" **unchecked** for a first run against a single-assessment
   document; check it if you're specifically testing the long-report or
   integrated-report split path.
3. Submit. You're redirected to the batch's status page
   (`/studies/upload/<id>/`), which now shows `split_status: Detecting`.
   This is `detect_corpus_task` running on the Celery worker — refresh the
   page (there's no auto-refresh/websocket) to see it advance.
4. **What happens next depends on what `build_corpora()` found:**
   - **Exactly one corpus, and you didn't check the split box** → status
     jumps straight to `Confirmed` and a `Study`/`PipelineRun` starts
     automatically (no review step) — this is the ordinary single-study
     path.
   - **Anything else** (you checked the split box, or the PDF is a
     long-report split, or an integrated multi-assessment report) →
     status becomes `Awaiting confirmation`, and the status page shows a
     **"Review and confirm"** link to `/studies/upload/<id>/review/`.
     There you see every detected corpus (title, category, page ranges,
     shared-vs-specific pages, a text preview, detection warnings),
     include/exclude checkboxes, editable titles, and three actions:
     **Confirm selected corpora**, **Process original PDF as one corpus
     instead**, and **Cancel**. Confirming creates one `Study` +
     `PipelineRun` per included corpus and starts GPT-5 processing for
     each.
   - **Detection fails** (bad PDF, OCR timeout, etc.) → status becomes
     `Failed`, with the error message and a failure-history table shown
     on the status page, plus a **Retry corpus detection** button (up to
     `CORPUS_DETECTION_MAX_RETRIES` times) and an always-available
     **Process original PDF as one corpus** fallback.
5. Once a `Study` has a running/completed `PipelineRun`, the status page
   lists it — click through to `/studies/<study_id>/` (study detail: all
   runs for that study) and then `/studies/<study_id>/runs/<run_id>/`
   (run detail: every answer, in question order, plus usage/cost
   totals and any failed/timed-out LLM calls for that run).
6. From the run detail page, **Download full conversation (.txt)** and
   **Download answers only (.txt)** give you the two files the parity
   checklist (§10, `docs/OUTPUT_PARITY_CHECKLIST.md`) compares against
   the legacy pipeline's own two output files (§10a below).
7. You can also browse everything via `/studies/` (every study you can
   see) or `/admin/` (staff-only, every table directly, including raw
   `LLMCallLog`/`StudyPage` rows).

## 9. Where to check errors

**Most reliable, always populated regardless of console/log
configuration** — the database itself, via `/admin/` or the study/run
pages:
- `UploadBatch.split_error_message` (current) and
  `UploadBatch.detection_failure_history` (every past failure, with
  timestamps) — corpus-detection failures.
- `Study.error_message` / `PipelineRun.error_message` — a run that failed
  outright.
- `LLMCallLog` rows (visible inline on a `PipelineRun` in `/admin/`, or
  via the run detail page's "Failed / timed-out calls" section) — one row
  per LLM call, `status` (`success`/`failed`/`timeout`) and
  `error_message` per call, not just an aggregate count.
- `StudyPage.ocr_attempted`/`ocr_succeeded`/`ocr_error` — per-page OCR
  outcome (a failed/timed-out OCR call on one page doesn't fail the whole
  study — it falls back to non-OCR text, per decision 5 — but the failure
  is still recorded here).

**Celery worker console** — the terminal running
`celery -A dataopt worker --loglevel=info`:
- Any genuinely unhandled exception in a task prints a full Python
  traceback here regardless of log level (Celery's own task-failure
  tracing, independent of anything below).
- The app's own `logging.getLogger(__name__)` calls in
  `studies/tasks.py`, `studies/corpus_detection.py`,
  `studies/llm_integration.py`, and `studies/services.py` (task
  start/completion, retry attempts, cost-limit halts, failed LLM calls)
  — **only appear here if the worker is started with `--loglevel=info`
  or lower.** At the default `--loglevel=warning`, only the warning-level
  ones (failures, cost-limit halts) show; the routine info-level ones
  (task started, run completed, retry claimed) are silently dropped. See
  the note right after this list — this is a real gap I found while
  writing this guide and have not fixed without asking you first.

**Django dev server console / browser** — `manage.py runserver`'s
terminal, and the browser itself when `DJANGO_DEBUG=true`:
- An unhandled exception in a *view* (as opposed to a task) shows Django's
  full debug error page in the browser.
- The same app-level `logging` calls made directly inside a view (not via
  a task) — e.g. `corpus_detection.confirm_detected_corpora`/
  `cancel_review`/`retry_corpus_detection_claim`'s own `logger.info(...)`
  calls, which run synchronously in the web request before any Celery
  task is enqueued — currently **do not appear anywhere**, console or
  otherwise, under `manage.py runserver`'s default configuration (no
  `LOGGING` dict is defined in `dataopt/settings/`). Only `logger.warning`
  calls from view code would show, via Python's WARNING-level
  last-resort handler on stderr.

> **⚠ Gap found while writing this guide, not fixed yet — see the note
> after this section before you rely on log output for troubleshooting.**

**OCR-specific**: beyond `StudyPage.ocr_error` above, a Docling exception
during OCR also appears in the Celery worker console the same way any
other exception inside a task would, since `_run_ocr_with_timeout` lets
genuine Docling errors propagate (only the timeout itself is caught and
recorded per-page).

### Note: the logging-visibility gap I found, and what I'd propose

While confirming what you'd actually see under §9, I tested it directly
(no `LOGGING` setting exists anywhere in `dataopt/settings/`) and found:
Python's root logger has no configured handler, so any `logger.info(...)`
call anywhere in the app — the majority of what Phase 6 added — is
silently dropped unless it happens inside a Celery task *and* the worker
was started with `--loglevel=info`. Calls made synchronously from a view
(the claim/confirm/cancel/retry service calls, before any task is
enqueued) never appear anywhere under `manage.py runserver`, at any log
level, since Celery isn't involved in a web request and nothing else
configures a handler.

This doesn't block your testing — the database fields above are always
populated regardless, which is why they're listed first — but it does
mean the log stream is currently much thinner than `docs/PHASE_STATUS.md`
implies ("visible in the log stream, not just the database"). I have
**not** changed anything about this — it's a `dataopt/settings/base.py`
change (adding a `LOGGING` dict with a console handler at `INFO` for the
`studies` logger namespace), which is a real, if small, runtime-behavior
change, and you told me to ask before making one of those. Let me know if
you'd like me to add it before you start testing, after, or not at all.
