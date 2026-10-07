# Azure Container Apps deployment

How D.A.T.A OPT runs on Azure Container Apps, for **non-sensitive testing**.
The Linux single-server setup is in `LINUX_TEST_DEPLOYMENT.md`; nothing here
changes how that server behaves.

## What runs where

| Container app | Image | Size | Copies | Ingress |
|---|---|---|---|---|
| `dataopt-web` | `dataopt-web:<commit>` (Dockerfile target `web`) | 0.5 CPU, 1 GiB | min **0**, max 1 | External, HTTPS only, port 8000 |
| `dataopt-worker` | `dataopt-worker:<commit>` (Dockerfile target `worker`) | 2 CPU, 4 GiB | min 1, max 1 | None |
| `dataopt-redis` | `redis:7-alpine` (public) | 0.25 CPU, 0.5 GiB | min 1, max 1 | Internal TCP, port 6379 |

All three are in one Container Apps environment in the landing zone's `app`
subnet (delegated to `Microsoft.App/environments`). PostgreSQL, Azure OpenAI
and the Azure Files share are reached through private endpoints in the
`data` subnet.

**Why two images.** The website never runs OCR, embeddings or AI calls; it
saves the upload, records it and queues a job. So its image
(`requirements-web.txt`) leaves out PyTorch, sentence-transformers, Docling
and the downloaded models: about 0.6 GB on disk and about 140 MB to
download, against roughly 2.5 to 3 GB for the worker. That keeps the first
page after an idle period quick, which matters because the website scales
to zero. `studies/tests/test_web_imports.py` fails if a change makes the
website's code import a worker-only package again.

**Why only the website scales to zero.** Redis holds the job queue in
memory, so stopping it loses queued jobs, and the worker would not wake up
when a job arrived. Container Apps stops the website a few minutes after
the last request and starts it again on the next one.

## Settings and secrets

Secrets are stored encrypted and passed to the app by reference; plain
environment variables are visible to anyone who can read the app's
settings. Anything that grants access is a secret.

| Name | Kind | Value | Apps |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | Secret | a long random string, generated once | web, worker |
| `POSTGRES_PASSWORD` | Secret | the PostgreSQL admin password | web, worker |
| `OPENAI_API_KEY` | Secret | Key 1 from the Azure OpenAI resource | worker (web too, harmless) |
| `CELERY_BROKER_URL` | Secret | `redis://:<REDIS_PASSWORD>@dataopt-redis:6379/0` | web, worker |
| `CELERY_RESULT_BACKEND` | Secret | same as `CELERY_BROKER_URL` | web, worker |
| `REDIS_PASSWORD` | Secret | a random string, generated once | redis |
| `DJANGO_SETTINGS_MODULE` | Plain | `dataopt.settings.prod` (also the image default) | web, worker |
| `DJANGO_ALLOWED_HOSTS` | Plain | the web app's address, `dataopt-web.<id>.<region>.azurecontainerapps.io` | web, worker |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Plain | leave unset unless a custom domain is added | web |
| `POSTGRES_HOST` | Plain | `<server>.postgres.database.azure.com` | web, worker |
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PORT` | Plain | `data_opt` / the admin user / `5432` | web, worker |
| `POSTGRES_SSLMODE` | Plain | `require` | web, worker |
| `MEDIA_ROOT` | Plain | `/mnt/media` | web, worker |
| `OPENAI_BASE_URL` | Plain | `https://<resource>.openai.azure.com/openai/v1/` | worker (web too) |
| `OPENAI_MODEL` | Plain | the Azure OpenAI **deployment name** | worker (web too) |
| `OPENAI_PRICE_INPUT_USD_PER_1M` / `OPENAI_PRICE_OUTPUT_USD_PER_1M` | Plain | from Microsoft's Azure OpenAI pricing page (Global deployment type) | worker |

The redis app's command is
`sh -c 'exec redis-server --requirepass "$REDIS_PASSWORD" --save "" --appendonly no'`.

The storage account key and the registry password are not app settings:
the storage key is entered once on the environment (below), and the
registry password as each app's registry login.

## Uploaded files: the Azure Files share

1. On the Container Apps environment, **Azure Files**: add a storage entry
   named `mediashare` with the storage account, share `media`, its key, and
   access mode Read/Write.
2. On both `dataopt-web` and `dataopt-worker`, add a volume of type Azure
   file share using `mediashare`, mounted at `/mnt/media`, with mount options
   `uid=1000,gid=1000,dir_mode=0770,file_mode=0660`. The images run as user
   1000; without those options the share is owned by root and uploads fail.

With `MEDIA_ROOT=/mnt/media`, an upload is saved to
`/mnt/media/uploads/<yyyy>/<mm>/<dd>/<file>.pdf`, and the worker opens the
same path.

## HTTPS

Ingress ends HTTPS and forwards plain HTTP with `X-Forwarded-Proto: https`.
`dataopt/settings/prod.py` trusts that header (`SECURE_PROXY_SSL_HEADER`),
which is safe only because ingress is the container's sole way in. Set
ingress to HTTPS only (insecure connections off). Keep the default TCP
health probes: an HTTP probe would get a redirect and be marked as failing.
WhiteNoise serves the admin's CSS and JS from inside the web image.

## Deploying

`scripts/azure/deploy.sh` builds both images from the same commit with
`az acr build` (in Azure, nothing to install locally), then updates the
worker and then the website to that commit. Run it from Azure Cloud Shell in
a clone of the repository:

```
git clone https://github.com/mzeinagh/D.A.T.A-OPT-Project.git
cd D.A.T.A-OPT-Project
git checkout <branch>
export RESOURCE_GROUP=<resource group>
export ACR_NAME=<registry name>
./scripts/azure/deploy.sh
```

- It stops if there are uncommitted changes, because the image tag is the
  commit.
- Both images are built before either app changes, so a failed build
  leaves both apps on the previous version.
- The worker build takes a while: it installs PyTorch and downloads the
  embedding and OCR models.
- At the end it prints the image each app runs and says whether the deploy
  added database migrations.

**First time only.** The apps don't exist yet, so run
`./scripts/azure/deploy.sh --build-only`. It builds both images and prints
their names, which you pick when creating the three apps with the sizes,
copies, settings and volume above.

## One-off commands

Run these from the **worker's** Console in the portal (it is always
running; the website may be scaled to zero):

```
python manage.py migrate
python manage.py createsuperuser
```

Then load the 11 questions and the default prompt configuration with the
script in `LINUX_TEST_DEPLOYMENT.md` section 8.1 (from `python manage.py
shell <<'PYEOF'` onward; skip the `cd`, `source` and `.env` lines, since
the container already has its settings).

## Checking Azure OpenAI before the first PDF

From the worker's Console:

```
python manage.py shell -c "from llm.factory import build_default_llm_client as b; r = b().invoke('Reply with OK'); print(r.status, r.text, r.error_message)"
```

`success OK None` means the address, key and deployment name are right.
