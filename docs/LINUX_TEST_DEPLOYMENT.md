# Deploying D.A.T.A OPT to a Linux VM — test/development environment

This document describes how to get the `django-app` application running on a
fresh Linux virtual machine. It is written from an actual deployment carried
out on 21 September 2026 onto a new DigitalOcean droplet, and every command,
path and package name below was either run during that deployment or checked
against the code in this repository.

It is not a generic Django guide. Where this application needs something
unusual — a CPU-only build of PyTorch, a graphics library for the OCR engine,
a hand-run script to populate two database tables — that is spelled out here,
because those are the steps that actually cost time.

**Placeholders.** Anything written as `<LIKE_THIS>` must be replaced with a
real value before the command is run. Never commit real values back into this
repository. The placeholders used are `<DB_PASSWORD>`, `<OPENAI_API_KEY>`,
`<DJANGO_SECRET_KEY>`, `<DROPLET_IP>` (the server's public address) and
`<YOUR_PUBLIC_IP>` (the address of the machine you browse from).

**Verified vs. not.** Sections 1–9 were carried out end to end and are
verified. The first full PDF run (section 10.6) had not been completed when
this document was written; that step is marked where it appears.

---

## 1. Purpose and scope

This is a **test/development** deployment. Its purpose is to confirm the
application runs correctly on Linux, not to serve real users.

Django is started with its built-in development server:

```
python manage.py runserver 0.0.0.0:8000
```

That server is single-threaded, reloads itself when files change, serves
static and uploaded files itself, and prints full Python tracebacks into the
browser when something fails. Those properties are exactly what you want while
testing and exactly what you must not expose to the public internet
permanently.

Deliberately **not** covered, because they were deliberately not done:

- No Gunicorn, uWSGI or any other WSGI/ASGI server
- No Nginx or other reverse proxy
- No HTTPS/TLS certificates
- No systemd service units — every process here is started by hand
- No `dataopt.settings.prod` — this runs on `dataopt.settings.dev`

A production deployment is a separate exercise with different settings, a
different web server and different security requirements.

---

## 2. VM requirements

### Operating system

**Ubuntu 24.04 LTS.** This is not a free choice. See section 13 for the full
explanation, but in short: this codebase requires Python 3.10 or newer, and
modern PyTorch requires glibc 2.28 or newer. Ubuntu 24.04 ships Python 3.12.3
and glibc 2.39. Ubuntu 18.04 **cannot** run this application and cannot be
made to.

Ubuntu 22.04 would also work (Python 3.10, glibc 2.35), but 24.04 was what we
used and what these commands were verified against.

### CPU architecture

**x86-64 (also called AMD64 or Intel/AMD).** All the wheels used here —
PyTorch, torchvision, Docling's models — are published for x86-64. ARM64
builds exist for some of them but were not tested.

If you deploy on DigitalOcean this needs no thought: DigitalOcean does not
currently sell ARM droplets at all. Their droplet CPUs are Intel Xeon and AMD
EPYC. On providers that do sell ARM (Hetzner, AWS Graviton, Oracle Ampere),
choose an x86-64 instance.

### CPU, RAM and disk

|             | Minimum       | Recommended   |
|-------------|---------------|---------------|
| vCPUs       | 2             | 4             |
| RAM         | 4 GB          | 8 GB          |
| Disk        | 80 GB         | 80 GB         |

The deployment described here ran on the minimum: a 2 vCPU / 4 GB / 80 GB
DigitalOcean Basic droplet.

**4 GB is genuinely enough, and this was measured rather than guessed.** After
loading the sentence-transformers embedding model and running Docling's OCR
over a page, 3.3 GB of RAM remained available and the 4 GB swap file (section
3) had been touched for 780 KB — effectively nothing. The untested case is a
long scanned document rather than a single page; watch memory during your
first large upload.

Disk actually used after a complete install was about 9 GB, so 80 GB is
generous. Do not go below ~25 GB: the virtual environment alone is 2.1 GB
because of PyTorch.

More RAM and CPU buy you faster OCR and the ability to process more than one
study at a time. They are not required for correctness.

### Network

The VM must be able to reach, outbound over HTTPS:

- `api.openai.com` — every question asked of a study is an API call
- `huggingface.co` — first-time download of the embedding model, and of
  Docling's OCR models on first OCR run

Inbound, you need TCP 22 (SSH) and TCP 8000 (the Django dev server). See
section 7 for restricting both.

---

## 3. Initial Linux setup

Everything in this section is run as `root` over SSH, or with `sudo` in front
of it if you use a non-root account.

### 3.1 Update the system

```
apt update && apt upgrade -y
```

### 3.2 Install the required system packages

Two separate groups. The first is the build and service tooling:

```
apt install -y python3-venv python3-dev build-essential \
  postgresql postgresql-contrib libpq-dev \
  redis-server git curl
```

- `python3-venv`, `python3-dev`, `build-essential` — needed to create a
  virtual environment and to compile any package that has no pre-built wheel
- `postgresql`, `postgresql-contrib`, `libpq-dev` — the database, plus the
  headers `psycopg2-binary` expects
- `redis-server` — Celery's message broker and result store
- `git`, `curl` — to fetch the code and to test connectivity

The second group is easy to miss and cost us a debugging cycle, so install it
now rather than discovering it later:

```
apt install -y libgl1 libglib2.0-0
```

`libgl1` is a graphics library that OpenCV needs. Docling imports OpenCV
indirectly through its table-detection models, so without `libgl1` the very
first OCR attempt dies with `ImportError: libGL.so.1: cannot open shared
object file`. It is a system library, not a Python package, so no amount of
`pip install` fixes it.

> A note on `tesseract-ocr`: during our deployment we also installed it while
> chasing a "No OCR engine found" error. It turned out that `libgl1` was the
> real fix, and that Docling then selected **RapidOCR** and downloaded its own
> PP-OCRv6 models. Tesseract appears to be unused. It is harmless to install
> but is not, as far as we could establish, required.

### 3.3 Add a swap file

The droplet had no swap. A 4 GB swap file is cheap insurance against a large
document briefly exceeding RAM, and it costs nothing when unused — ours sat at
780 KB after a full OCR run.

```
fallocate -l 4G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

The last line makes it survive a reboot. Confirm with:

```
swapon --show
free -h
```

### 3.4 Git

`git` was installed in 3.2. No further configuration is needed to clone a
public repository. If the repository is private, set up an SSH deploy key or
use a personal access token — that is outside this document's scope.

---

## 4. Application installation

### 4.1 Which branch

**The application lives on the `django-app` branch, not `main`.**

`main` is a superseded command-line prototype. `django-app` contains all of
`main`'s history plus the entire Django conversion. Cloning the default branch
gets you the wrong code.

The legacy prototype files (`main.py`, `config.py`, `nodes/`, `graph.py`,
`search/`, `utils.py`) are still present in the repository root on
`django-app`, but nothing in the Django application imports them. Editing them
will not change the web app.

> **At the time of writing**, three fixes made during this deployment — the
> login page, the logout button and the `/` route (see section 12.6–12.8) —
> are on the branch `claude/project-thread-tqgj20` and have not yet been
> merged into `django-app`. Until they are merged, check that branch out
> instead, or you will hit those three faults on a fresh deploy.

### 4.2 Clone the code

We used `/opt/dataopt` as the application directory.

```
cd /opt
git clone https://github.com/mzeinagh/D.A.T.A-OPT-Project.git dataopt
cd /opt/dataopt
git checkout django-app
```

### 4.3 Create and activate the virtual environment

```
cd /opt/dataopt
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
```

Your shell prompt gains a `(venv)` prefix. **Every** Python command in the
rest of this document assumes the virtual environment is active. If you open a
new terminal, run `source /opt/dataopt/venv/bin/activate` again.

### 4.4 Install CPU-only PyTorch — before anything else

This step must come **before** `pip install -r requirements.txt`, and getting
the order wrong is expensive.

```
pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
```

**Why.** `sentence-transformers` and `docling` both depend on PyTorch. On
Linux, the default PyTorch package on PyPI is the CUDA build: it drags in
roughly 8 GB of NVIDIA GPU libraries. A basic cloud VM has no GPU, so all of
that is dead weight — it fills the disk and buys nothing. The
`download.pytorch.org/whl/cpu` index serves builds compiled without CUDA.

Installing it first means that when `requirements.txt` later asks for
`sentence-transformers` and `docling`, pip sees PyTorch is already satisfied
and leaves it alone.

**Install `torchvision` at the same time and from the same index.** This is
the exact mistake we made: we installed only `torch` from the CPU index, and
`torchvision` then arrived later as a Docling dependency — from PyPI, built
for CUDA. The mismatched pair failed at import with `RuntimeError: operator
torchvision::nms does not exist`, followed by a confusing
`ModuleNotFoundError: Could not import module 'PreTrainedModel'`.

Verify both are CPU builds before continuing:

```
python -c "import torch, torchvision; print(torch.__version__, torchvision.__version__)"
```

Both version strings **must** end in `+cpu`, for example `2.14.0+cpu
0.25.0+cpu`. If either does not, reinstall that package from the CPU index.

### 4.5 Install the application dependencies

```
cd /opt/dataopt
pip install -r requirements.txt
```

This takes several minutes. Afterwards, confirm no GPU packages crept in:

```
pip list | grep -i nvidia
```

That should print nothing. A correct install is around 2.1 GB.

### 4.6 Download the embedding model

The repository tracks the *folder* `embeddings_local/all-MiniLM-L6-v2/` but
not the model weights inside it — `.gitignore` deliberately excludes
`*.safetensors`, `*.bin` and friends. You must fetch them once:

```
cd /opt/dataopt
pip install huggingface_hub
python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('sentence-transformers/all-MiniLM-L6-v2', local_dir='embeddings_local/all-MiniLM-L6-v2')
"
```

Verify it loads and reports the right dimensionality:

```
python -c "
from sentence_transformers import SentenceTransformer
m = SentenceTransformer('/opt/dataopt/embeddings_local/all-MiniLM-L6-v2')
print('embedding dimension:', m.encode(['x']).shape[-1])
"
```

Expect `embedding dimension: 384`.

An alternative is to leave `EMBEDDING_MODEL_PATH` set to the Hugging Face
model id `sentence-transformers/all-MiniLM-L6-v2` and let the library manage
its own cache. `core_pipeline/search/vector_store.py` passes the value
straight to `SentenceTransformer(...)`, which accepts either form.

---

## 5. Required services

Three services must be running: PostgreSQL, Redis and a Celery worker. There
is **no vector database** — retrieval uses an in-memory NumPy index plus a
hand-written BM25 implementation, fused by reciprocal rank fusion, rebuilt per
run. Nothing to install for it.

### 5.1 PostgreSQL

Installed in section 3.2, and Ubuntu starts it automatically. The application
hardcodes `django.db.backends.postgresql` in `dataopt/settings/base.py` — there
is no SQLite fallback, so this is not optional.

Create the role and database:

```
sudo -u postgres psql -c "CREATE USER data_opt WITH PASSWORD '<DB_PASSWORD>';"
sudo -u postgres psql -c "CREATE DATABASE data_opt OWNER data_opt;"
```

> **Customise before running:** replace `<DB_PASSWORD>` with a password you
> choose. You will need the same value in the `.env` file in section 6.

Making `data_opt` the *owner* of the database matters on PostgreSQL 15 and
later: the owner gets rights over the `public` schema, which is what Django's
migrations need in order to create tables.

Verify:

```
PGPASSWORD='<DB_PASSWORD>' psql -h localhost -U data_opt -d data_opt -c "SELECT current_user, current_database();"
```

Expect `data_opt | data_opt`.

### 5.2 Redis

Installed in section 3.2 and started automatically by Ubuntu. Redis is both
Celery's broker and its result backend, which is how the status-polling view
knows how a run is progressing.

Verify:

```
redis-cli ping
```

Expect `PONG`.

### 5.3 Celery

Celery is **not optional**. `studies/tasks.py` defines the task that does all
the real work — rehydrating the detected corpus, building the retrieval index
and running the 11-question loop. With no worker consuming the queue, an
upload sits at "Detecting" forever and nothing else ever happens. There is no
error message; it simply never progresses.

Starting it is covered in section 9.

### 5.4 Nothing else

No Node.js, no npm, no JavaScript build step: the interface is Django
templates only and there is no `package.json`. No message queue other than
Redis. No object storage — uploads are written to the local filesystem under
`media/`, which is also why the web process and the Celery worker must run on
the same machine (`studies/corpus_detection.py` passes a local file path to
the worker).

---

## 6. Application configuration

### 6.1 The `.env` file and an important caveat

Settings are read from environment variables by the helpers at the top of
`dataopt/settings/base.py`. **Nothing in the application loads a `.env` file
automatically** — there is no `python-dotenv` in `requirements.txt`. The file
is a convenience for you to load into your shell:

```
set -a && source /opt/dataopt/.env && set +a
```

You must run that line in **every** terminal before starting Django, starting
Celery, or running any `manage.py` command. Forgetting it is the single most
common cause of confusing failures — the application starts, then can't reach
the database or has no API key.

`.env.example` in the repository root lists every variable with comments and
is the authoritative reference. Copy it as a starting point:

```
cp /opt/dataopt/.env.example /opt/dataopt/.env
chmod 600 /opt/dataopt/.env
```

`chmod 600` means only the file's owner can read it. The file holds your
database password and your OpenAI key, so this matters. `.gitignore` already
excludes `.env`, so it will not be committed.

### 6.2 Generating the secret key

Do not invent one by hand and do not copy one from anywhere:

```
python3 -c "import secrets; print(secrets.token_urlsafe(50))"
```

Paste the output as `DJANGO_SECRET_KEY`.

### 6.3 Every variable, and what it does

| Variable | Required? | What it does |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | No | Which settings file to load. `manage.py` and `dataopt/celery.py` both default to `dataopt.settings.dev`. Set it to `dataopt.settings.dev` explicitly for clarity. |
| `DJANGO_SECRET_KEY` | Recommended | Signs session cookies and password-reset tokens. `dev.py` substitutes an insecure fallback if it is blank, so the app will start without it — but sessions are then trivially forgeable. Set it. |
| `DJANGO_DEBUG` | No | Defaults to `true` under `dev.py`. Must stay `true` here: `dataopt/urls.py` only serves uploaded files under `MEDIA_URL` when `DEBUG` is on, so uploads become invisible if you turn it off. |
| `DJANGO_ALLOWED_HOSTS` | **Yes** | Comma-separated hostnames/addresses Django will answer for. `dev.py` falls back to `localhost,127.0.0.1` only, so **the server's own IP must be listed** or every request from a browser is rejected with `DisallowedHost`. Use `<DROPLET_IP>,localhost,127.0.0.1`. |
| `POSTGRES_DB` | No | Database name. Defaults to `data_opt`. |
| `POSTGRES_USER` | No | Database role. Defaults to `data_opt`. |
| `POSTGRES_PASSWORD` | **Yes** | The `<DB_PASSWORD>` from section 5.1. Defaults to empty, which will not authenticate. |
| `POSTGRES_HOST` | No | Defaults to `localhost`. |
| `POSTGRES_PORT` | No | Defaults to `5432`. |
| `CELERY_BROKER_URL` | No | Defaults to `redis://localhost:6379/0`. |
| `CELERY_RESULT_BACKEND` | No | Defaults to `redis://localhost:6379/0`. |
| `EMBEDDING_MODEL_PATH` | Recommended | Directory holding the sentence-transformers model, or a Hugging Face model id. Defaults to the repo's `embeddings_local/all-MiniLM-L6-v2`. Set it to the absolute path `/opt/dataopt/embeddings_local/all-MiniLM-L6-v2`. |
| `HANDBOOK_PDF_PATH` | Recommended | Absolute path to the evaluator's handbook PDF, which the `retrieve_guide` pipeline node consults. The repository ships one in `dependants/`. |
| `OCR_TIMEOUT_SECONDS` | Recommended | How long a single OCR call may run. Default 60. **We set 300.** |
| `CORPUS_DETECTION_MAX_RETRIES` | No | How many times a failed corpus detection may be retried from the review page before only "process as one corpus" remains. Default 3. |
| `OPENAI_API_KEY` | **Yes** | Your `<OPENAI_API_KEY>`. Without it, uploads are detected and indexed but every question fails. |
| `OPENAI_MODEL` | No | Defaults to `gpt-5`. |
| `OPENAI_MAX_OUTPUT_TOKENS` | No | Default 1024. |
| `OPENAI_TIMEOUT_SECONDS` | No | Default 60. |
| `OPENAI_MAX_RETRIES` | No | Default 3. |
| `OPENAI_COST_LIMIT_ENABLED` | No | Default `false`. Enabling it without setting the limit below does nothing. |
| `OPENAI_COST_LIMIT_USD` | No | Per-run ceiling in US dollars. Only takes effect when the flag above is `true`. Worth setting for a first test run. |

**On `OCR_TIMEOUT_SECONDS`.** The default of 60 seconds is too short for
scanned documents on a 2-vCPU machine, where a single page of OCR can take
tens of seconds. We used 300. Note the limitation that
`core_pipeline/document_processor.py` documents about itself: the timeout is
implemented with `ThreadPoolExecutor.result(timeout=...)`, which stops
*waiting* for the OCR call but does not stop the call itself. An over-running
OCR keeps consuming CPU in the background. Setting a realistic timeout is
better than relying on it to rescue you.

**On vector storage.** There is no vector store to configure. The index is
built in memory per run from `EMBEDDING_MODEL_PATH`, and nothing persists
between runs.

### 6.4 Worked example

```
cat > /opt/dataopt/.env <<'EOF'
DJANGO_SETTINGS_MODULE=dataopt.settings.dev
DJANGO_SECRET_KEY=<DJANGO_SECRET_KEY>
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=<DROPLET_IP>,localhost,127.0.0.1

POSTGRES_DB=data_opt
POSTGRES_USER=data_opt
POSTGRES_PASSWORD=<DB_PASSWORD>
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=redis://localhost:6379/0

EMBEDDING_MODEL_PATH=/opt/dataopt/embeddings_local/all-MiniLM-L6-v2
HANDBOOK_PDF_PATH=/opt/dataopt/dependants/eau1-handbook-structured.pdf

OCR_TIMEOUT_SECONDS=300
CORPUS_DETECTION_MAX_RETRIES=3

OPENAI_API_KEY=<OPENAI_API_KEY>
OPENAI_MODEL=gpt-5
OPENAI_MAX_OUTPUT_TOKENS=1024
OPENAI_TIMEOUT_SECONDS=60
OPENAI_MAX_RETRIES=3
OPENAI_COST_LIMIT_ENABLED=false
OPENAI_COST_LIMIT_USD=
EOF
chmod 600 /opt/dataopt/.env
```

> **Customise before running:** all four `<...>` placeholders.

> **A note on the handbook filename.** It was originally
> `Structured EAU1 _student_ handbook (2).pdf`, and was renamed to
> `eau1-handbook-structured.pdf` precisely because spaces and brackets in a
> path have to be quoted everywhere they appear — and an unquoted value is
> silently truncated at the first space. If you point `HANDBOOK_PDF_PATH` at
> a file whose name contains spaces, wrap the value in double quotes.

---

## 7. Security for the test environment

The Django development server prints full tracebacks — including snippets of
settings — into the browser on any unhandled error, has had no security
review as a public-facing server, and here sits in front of an application
that spends money on your OpenAI account. Anyone who finds the address can
reach it.

**Restrict access by IP.** The cleanest way on DigitalOcean is a cloud
firewall, which filters traffic before it reaches the droplet.

1. In the DigitalOcean control panel, go to **Networking → Firewalls → Create
   Firewall**.
2. Under **Inbound Rules**, delete the defaults and add exactly two:
   - `SSH` / TCP / port `22`, Sources: `<YOUR_PUBLIC_IP>`
   - `Custom` / TCP / port `8000`, Sources: `<YOUR_PUBLIC_IP>`
3. Leave **Outbound Rules** at their defaults — the VM needs to reach
   `api.openai.com` and `huggingface.co`.
4. Apply the firewall to the droplet.

Find `<YOUR_PUBLIC_IP>` by visiting `https://ifconfig.me` from the machine you
browse on. If your connection has a changing address, you will need to update
the rule when it changes.

The equivalent on the droplet itself, if you would rather not use a cloud
firewall:

```
ufw allow from <YOUR_PUBLIC_IP> to any port 22 proto tcp
ufw allow from <YOUR_PUBLIC_IP> to any port 8000 proto tcp
ufw enable
```

> **Be careful:** `ufw enable` with a wrong address in those rules locks you
> out of your own server. The cloud firewall is safer because it can be edited
> from the control panel even when you cannot log in. If you use `ufw`, keep
> an active SSH session open until you have confirmed a second one works.

> **What we actually did:** no firewall was configured during this deployment
> — it was explicitly skipped, so port 8000 was open to the internet. If you
> follow this document, do the firewall. It takes two minutes.

**An SSH tunnel instead of an open port.** A neat alternative that needs no
firewall rule for 8000 at all: connect with

```
ssh -L 8000:localhost:8000 root@<DROPLET_IP>
```

and then browse to `http://localhost:8000/` on your own machine. Port 8000
never has to be open to the internet. Only port 22 does.

**SSH keys.** An SSH key pair is meaningfully more secure than a password:
keys are not guessable, and automated scanners hammer port 22 constantly on
any public address. Choose "SSH Key" when creating the droplet rather than
adding one afterwards — it is much easier. If you use a root password anyway
(as we did), make it long and random, and restrict port 22 by IP as above.
`apt install -y fail2ban` will additionally block addresses after repeated
failed logins.

---

## 8. Database preparation

With the virtual environment active and `.env` loaded:

```
cd /opt/dataopt
source venv/bin/activate
set -a && source /opt/dataopt/.env && set +a

python manage.py check
python manage.py migrate
python manage.py makemigrations --check --dry-run
python manage.py createsuperuser
```

What each does, and what to expect:

- **`check`** imports the entire application — every model, view, form and
  pipeline module. It is the single best early test that the install is sound,
  because anything missing surfaces here rather than halfway through a run.
  Expect `System check identified no issues (0 silenced).`
- **`migrate`** creates the tables. Expect 22 migrations to apply, the last
  four of them `studies.0001` through `studies.0004`.
- **`makemigrations --check --dry-run`** confirms the database now matches the
  models. Expect `No changes detected`. If it reports changes, something is
  out of step — investigate before going further.
- **`createsuperuser`** creates your login. Leaving the username blank accepts
  the shell user's name, which as root means the account is called `root`.
  Note the username prompt **echoes what you type**, so do not paste a
  password into it.

### 8.1 Seed the questions and the prompt configuration — required

Two tables must be populated before any PDF can be processed, and there is no
management command to do it. This is deliberate: question governance is
admin-only by design. The text comes from `core_pipeline/questions.py`, which
is already in the repository and contains exactly 11 questions.

```
cd /opt/dataopt
source venv/bin/activate
set -a && source /opt/dataopt/.env && set +a

python manage.py shell <<'PYEOF'
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

Safe to run twice; it checks before inserting. Expect `Created 11 questions.`
and `Created and activated 'default' PromptConfig.`

**Exactly one `PromptConfig` must be active.** A run refuses to start
otherwise, raising `NoActivePromptConfigError` inside the Celery worker —
where, because this project has no `LOGGING` configuration, the failure is
easy to miss entirely.

---

## 9. Starting the application

Two processes: a Celery worker and the Django server. Both are started from
`/opt/dataopt`, both need the virtual environment active, and both need `.env`
loaded into the shell first.

### 9.1 Do you need separate terminals?

Not if you start them in the background with `nohup`, which is what we
settled on and what is shown below. Each process writes to a log file you can
read whenever you like, and both survive you closing the SSH session.

The alternative is one terminal per process — either several SSH connections,
or `tmux`. Run in the foreground, a process dies the moment you press Ctrl+C
or close the window. That caught us out: the server appeared unreachable when
in fact it had simply been stopped.

### 9.2 Start the Celery worker

```
cd /opt/dataopt
source venv/bin/activate
set -a && source /opt/dataopt/.env && set +a

nohup celery -A dataopt worker -l info --concurrency=1 > /opt/dataopt/celery.log 2>&1 &
sleep 10 && tail -30 /opt/dataopt/celery.log
```

`--concurrency=1` is a deliberate choice for a 4 GB machine. Celery defaults
to one job per CPU, so on 2 vCPUs two studies could process simultaneously —
each loading its own copy of the embedding model and the OCR models. That is
the one realistic way to run this machine out of memory. One at a time is
right for a test environment; raise it if you have the RAM and want
throughput.

**Start only one worker.** Running that command twice gives you two workers
competing for the same queue and interleaving their output into the same log
file, which is confusing to debug. Section 12.5 covers cleaning that up.

### 9.3 Start Django

```
cd /opt/dataopt
source venv/bin/activate
set -a && source /opt/dataopt/.env && set +a

nohup python manage.py runserver 0.0.0.0:8000 > /opt/dataopt/django.log 2>&1 &
sleep 5 && tail -10 /opt/dataopt/django.log
```

**`0.0.0.0:8000` is not optional.** Plain `python manage.py runserver` listens
on `127.0.0.1` only and refuses every connection from outside the machine.
`0.0.0.0` means "accept on all network interfaces".

---

## 10. Verification

Run these in order. Each one isolates a different layer, so the first failure
tells you where to look.

### 10.1 PostgreSQL

```
systemctl is-active postgresql
PGPASSWORD='<DB_PASSWORD>' psql -h localhost -U data_opt -d data_opt -c "SELECT current_user, current_database();"
```

Expect `active`, then `data_opt | data_opt`.

### 10.2 Redis

```
systemctl is-active redis-server
redis-cli ping
```

Expect `active`, then `PONG`.

### 10.3 Celery

```
tail -30 /opt/dataopt/celery.log
ps aux | grep '[c]elery' | wc -l
```

In the log, look for `celery@<hostname> ready.` and a `[tasks]` list
containing four `studies.tasks.*` entries. The process count should be **2**
for a single worker at `--concurrency=1` (one manager, one helper). At the
default concurrency on 2 vCPUs it would be 3. Six means you started two
workers.

### 10.4 Django

```
ss -tlnp | grep 8000
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/admin/
curl -sS -o /dev/null -w '%{http_code}\n' http://<DROPLET_IP>:8000/admin/
```

`ss` must show `0.0.0.0:8000`. Both `curl` calls should print `302` — Django
redirecting an anonymous request to the login page, which is the correct
answer.

If the first prints `302` and the second does not, the application is fine and
something in the network is blocking port 8000.

### 10.5 From a browser

Browse to `http://<DROPLET_IP>:8000/`. Sign in with the superuser from
section 8.

- `/` — redirects to the study list
- `/studies/` — the study list, empty on a fresh install
- `/studies/upload/` — the upload form
- `/admin/` — Django's admin interface

Confirm the seeding worked:

- `/admin/studies/question/` — 11 rows
- `/admin/studies/promptconfig/` — one row, shown as active

> **Do not test with `localhost`.** In a browser on your own laptop,
> `localhost` means your laptop, not the server. We lost time to exactly this:
> a `localhost:8000` test appeared to succeed while the server was in fact
> not running at all.

### 10.6 End-to-end test — not yet verified

> This step had not been completed at the time of writing. Everything above is
> verified; the following is the intended procedure from the repository's own
> `docs/END_TO_END_TESTING_GUIDE.md`.

1. Set a low spending cap on your OpenAI account first. A single study is
   roughly 33 API calls — three per question across 11 questions.
2. Go to `/studies/upload/` and upload one PDF. The limit is 100 MB
   (`studies/forms.py`).
3. The page moves to a status view while corpus detection runs. A scanned
   document triggers OCR here, which is slow — Docling also downloads its
   RapidOCR models on the very first run.
4. Review the detected corpora and confirm them.
5. The pipeline runs the 11 questions. Watch `/opt/dataopt/celery.log`.
6. Check the answers on the study page, and try the export links.

If an upload stays at "Detecting" indefinitely, the Celery worker is not
running or not consuming the queue. That is the first thing to check, always.

---

## 11. Stopping and restarting

### 11.1 Stop

```
pkill -f "manage.py runserver"
pkill -f "celery -A dataopt"
```

Confirm:

```
ps aux | grep -E '[c]elery|[r]unserver'
```

Nothing should be listed. Stopping mid-run abandons that run; the database
row will stay in whatever state it had reached.

### 11.2 What restarts by itself, and what does not

| Component | After a reboot |
|---|---|
| PostgreSQL | Starts automatically (systemd) |
| Redis | Starts automatically (systemd) |
| Swap file | Re-enabled automatically, via the `/etc/fstab` line from 3.3 |
| **Celery worker** | **Does not start. Start it by hand.** |
| **Django server** | **Does not start. Start it by hand.** |

This is a consequence of the test-environment scope: no systemd service units
were created for the application. A production deployment would have them.

### 11.3 Restart after a reboot or disconnect

```
cd /opt/dataopt
source venv/bin/activate
set -a && source /opt/dataopt/.env && set +a

nohup celery -A dataopt worker -l info --concurrency=1 > /opt/dataopt/celery.log 2>&1 &
nohup python manage.py runserver 0.0.0.0:8000 > /opt/dataopt/django.log 2>&1 &

sleep 10
tail -5 /opt/dataopt/celery.log
tail -5 /opt/dataopt/django.log
```

Then re-run the checks in section 10.

### 11.4 Settings changes need a restart

Template changes are picked up automatically — `runserver` watches files and
reloads. Changes to `.env` or to anything under `dataopt/settings/` are
**not**: those are read once at startup. After editing either, stop and start
the server.

---

## 12. Troubleshooting

Everything in 12.1–12.8 is a problem we actually hit during this deployment.

### 12.1 `RuntimeError: operator torchvision::nms does not exist`

Often followed by `ModuleNotFoundError: Could not import module
'PreTrainedModel'`, which is misleading — the real fault is below it.

**Cause.** `torch` and `torchvision` from different builds: CPU-only torch
paired with a CUDA torchvision that arrived as a dependency from PyPI.

**Fix.**

```
pip uninstall -y torchvision
pip install --index-url https://download.pytorch.org/whl/cpu torchvision
python -c "import torch, torchvision; print(torch.__version__, torchvision.__version__)"
```

Both versions must end in `+cpu`.

### 12.2 `ImportError: libGL.so.1: cannot open shared object file`

**Cause.** Docling's table-detection models import OpenCV, which needs a
system graphics library that a minimal server image does not include. This is
not a Python problem and `pip` cannot fix it.

**Fix.** `apt install -y libgl1 libglib2.0-0`

### 12.3 "No OCR engine found"

Appeared alongside 12.2 and was resolved by the same `libgl1` install. Docling
then selected RapidOCR and downloaded its PP-OCRv6 models on first use, which
needs outbound access to `huggingface.co`. We also installed `tesseract-ocr`
while diagnosing this, but it appears not to have been the fix and appears not
to be used.

### 12.4 The browser says "This site can't be reached"

Work through it in this order:

```
hostname                                                              # am I on the server?
ss -tlnp | grep 8000                                                  # is anything listening?
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/admin/  # does it answer locally?
sudo ufw status                                                       # is a firewall in the way?
```

- `ss` prints nothing → Django is not running. Most likely it was started in
  the foreground and stopped when its terminal closed. Restart per 11.3.
- `ss` shows `127.0.0.1:8000` → started without `0.0.0.0:8000`. Restart it
  correctly.
- Local `curl` gives `302` but the browser cannot connect → something between
  your machine and the server is blocking port 8000: a cloud firewall, `ufw`,
  or your own network's outbound rules.
- The browser shows a Django page saying `DisallowedHost` → the server's IP is
  missing from `DJANGO_ALLOWED_HOSTS`. Fix `.env` and restart.

The exact browser wording distinguishes two of these: "took too long to
respond" means traffic is being filtered; "refused to connect" means nothing
is listening.

### 12.5 Two Celery workers at once

**Symptom.** `jobs` shows two, `ps aux | grep '[c]elery'` shows six processes,
and the log file is interleaved nonsense. Six processes is two workers, not
six — each worker is one manager plus one helper per CPU.

**Fix.**

```
pkill -f 'celery -A dataopt'
sleep 2
ps aux | grep '[c]elery' | wc -l     # expect 0
> /opt/dataopt/celery.log
```

Then start exactly one, per section 9.2.

### 12.6 `405 Method Not Allowed` when logging out

**Cause.** Django 5 removed GET support from `LogoutView` — logging out must
be a POST. `templates/base.html` used a plain link.

**Fix.** Replace the link with a small POST form carrying the CSRF token.
Fixed on branch `claude/project-thread-tqgj20`.

### 12.7 `TemplateDoesNotExist: registration/login.html`

**Cause.** Every view is behind `@login_required` and `LOGIN_URL` points at
`/accounts/login/`, but the project had no login template.

**Fix.** Added `templates/registration/login.html`, on the same branch. Until
that branch is merged, the workaround is to sign in at `/admin/` instead —
Django's admin ships its own login template, and the resulting session
satisfies `@login_required` everywhere else.

### 12.8 A 404 at `/` immediately after signing in

**Cause.** `LOGIN_REDIRECT_URL` is `/`, and nothing was routed there.

**Fix.** A redirect from `/` to the study list in `dataopt/urls.py`, on the
same branch.

Related: logging out landed on an admin-styled page, because `LogoutView` with
no `LOGOUT_REDIRECT_URL` renders `registration/logged_out.html`, and the only
copy of that template in the project comes from `django.contrib.admin`. Fixed
by setting `LOGOUT_REDIRECT_URL = "/accounts/login/"`.

### 12.9 An upload never leaves "Detecting"

The Celery worker is not running, or is not consuming the queue. Check
`redis-cli ping`, then the worker per 10.3. There will be no error message on
the page.

### 12.10 Warnings that are harmless

These all appeared during our deployment and none indicates a fault:

- `NNPACK: Unsupported hardware` — an optional CPU accelerator this processor
  does not support; PyTorch falls back correctly
- `Preset 'granite_vision_v4' already registered` — Docling registering a
  model preset twice
- A warning about `HF_TOKEN` and rate limits — anonymous Hugging Face
  downloads are rate-limited but work
- A `padding='same'` UserWarning from within the OCR models
- `Not Found: /favicon.ico` in the Django log — the browser asking for a tab
  icon that does not exist

### 12.11 Useful diagnostic commands

```
free -h                                    # memory and swap
df -h /                                    # disk
systemctl is-active postgresql redis-server
redis-cli ping
ss -tlnp | grep 8000
ps aux | grep -E '[c]elery|[r]unserver'
tail -50 /opt/dataopt/django.log
tail -50 /opt/dataopt/celery.log
sudo -u postgres psql -c "\l"              # list databases
python manage.py check                     # imports the whole application
pip list | grep -i nvidia                  # should print nothing
```

**A caveat on logging.** This project has no `LOGGING` configuration in its
settings, so `logger.info()` calls inside the pipeline are discarded. The
Celery log at `-l info` is therefore your main window into a run. The
repository's `docs/END_TO_END_TESTING_GUIDE.md` §9 documents this gap.

---

## 13. Important notes and lessons learned

**Ubuntu 18.04 cannot run this application.** This was established three ways
before the machine was abandoned, and it is not a matter of trying harder:

1. Fifteen files in this repository use PEP 604 union syntax (`str | None`)
   without `from __future__ import annotations`. That requires Python 3.10.
   Ubuntu 18.04 ships Python 3.6, and the `deadsnakes` archive tops out at
   3.10 for that release — which sounds survivable until point 3.
2. PyTorch moved its Linux wheels to the `manylinux_2_28` standard in November
   2024, requiring glibc 2.28 or newer. Ubuntu 18.04 has glibc 2.27. No
   version of PyTorch new enough for current `sentence-transformers` and
   `docling` will install.
3. glibc is not something you upgrade on a live system.

A newer VM is the answer. Ubuntu 24.04 has Python 3.12.3 and glibc 2.39.

**Python 3.10 is a hard floor**, for the reason in point 1 above. 3.12 is what
we used.

**CPU vs GPU PyTorch is the most expensive single mistake available here.**
The default PyPI PyTorch on Linux is the CUDA build, around 8 GB of NVIDIA
libraries that a GPU-less VM cannot use. Install `torch` *and* `torchvision`
from `https://download.pytorch.org/whl/cpu` before `requirements.txt`, and
check both report `+cpu`. Done correctly the virtual environment is 2.1 GB
with no NVIDIA packages at all.

**4 GB of RAM is enough, measured rather than assumed.** After a full model
load and an OCR run, 3.3 GB remained available and swap usage was 780 KB. Keep
the swap file anyway, and keep Celery at `--concurrency=1` so two studies
cannot load two copies of the models simultaneously.

**Some system libraries are not Python packages.** `libgl1` is the example
that cost us a cycle. When an `ImportError` names a `.so` file, reach for
`apt`, not `pip`.

**Nothing loads `.env` for you.** There is no `python-dotenv` in this project.
`set -a && source /opt/dataopt/.env && set +a` goes in every shell, before
every command.

**The server's IP must be in `DJANGO_ALLOWED_HOSTS`**, or Django rejects every
browser request even though it is running perfectly.

**Foreground processes die with their terminal.** Use `nohup ... &` with a log
file, or `tmux`.

**Test with the server's address, never `localhost`.** `localhost` in a
browser on your laptop means your laptop.

**`requirements.txt` is effectively unpinned.** Only Django has a version
constraint. A fresh install months from now may resolve different versions of
PyTorch, Docling and the rest, and may not behave identically. Recording the
working set with `pip freeze > requirements.lock` after a successful install
would make this reproducible; that has not been done yet.

**This application had never been run for real before this deployment.** Per
`docs/END_TO_END_TESTING_GUIDE.md`, no live OpenAI call and no successful
model load had ever happened. Expect first-run surprises, and read errors
carefully rather than assuming the environment is at fault.

---

## Quick deployment checklist

For when you already understand the above. Every step assumes
`cd /opt/dataopt && source venv/bin/activate && set -a && source .env && set +a`
unless stated otherwise.

```
# 1. System
apt update && apt upgrade -y
apt install -y python3-venv python3-dev build-essential \
  postgresql postgresql-contrib libpq-dev redis-server git curl
apt install -y libgl1 libglib2.0-0

# 2. Swap
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab

# 3. Code  (note the branch)
cd /opt && git clone https://github.com/mzeinagh/D.A.T.A-OPT-Project.git dataopt
cd /opt/dataopt && git checkout django-app

# 4. Python  (CPU torch FIRST)
python3 -m venv venv && source venv/bin/activate && pip install --upgrade pip
pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
pip install -r requirements.txt
pip list | grep -i nvidia          # must print nothing

# 5. Embedding model
pip install huggingface_hub
python3 -c "from huggingface_hub import snapshot_download; snapshot_download('sentence-transformers/all-MiniLM-L6-v2', local_dir='embeddings_local/all-MiniLM-L6-v2')"

# 6. Database
sudo -u postgres psql -c "CREATE USER data_opt WITH PASSWORD '<DB_PASSWORD>';"
sudo -u postgres psql -c "CREATE DATABASE data_opt OWNER data_opt;"

# 7. Config
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(50))"    # -> DJANGO_SECRET_KEY
#   edit .env: SECRET_KEY, ALLOWED_HOSTS(<DROPLET_IP>), DB password,
#              OPENAI_API_KEY, EMBEDDING_MODEL_PATH, HANDBOOK_PDF_PATH,
#              OCR_TIMEOUT_SECONDS=300
set -a && source .env && set +a

# 8. Firewall: DigitalOcean panel, ports 22 and 8000, source <YOUR_PUBLIC_IP>

# 9. Migrate and seed
python manage.py check && python manage.py migrate
python manage.py makemigrations --check --dry-run
python manage.py createsuperuser
#   then run the seeding script from section 8.1  -> 11 questions + 1 active PromptConfig

# 10. Start
nohup celery -A dataopt worker -l info --concurrency=1 > celery.log 2>&1 &
nohup python manage.py runserver 0.0.0.0:8000 > django.log 2>&1 &

# 11. Verify
redis-cli ping                                                     # PONG
tail -30 celery.log                                                # "ready." + 4 tasks
ss -tlnp | grep 8000                                               # 0.0.0.0:8000
curl -sS -o /dev/null -w '%{http_code}\n' http://<DROPLET_IP>:8000/admin/   # 302
#   browse http://<DROPLET_IP>:8000/ and sign in
```
