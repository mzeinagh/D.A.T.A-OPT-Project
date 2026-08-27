"""Settings shared by every environment.

Nothing machine-specific or secret lives here — every path that used to be
hardcoded in the old `config.py` (chats_dir, pdf_dir, handbook_dir,
embedding_model_fp) and every LLM parameter is read from the environment,
per the "do not hardcode" decision for the OpenAI integration. `dev.py` /
`prod.py` only add environment-specific overrides (DEBUG, ALLOWED_HOSTS,
logging verbosity) — they do not redefine anything env-driven here.
"""
import os
from pathlib import Path


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def env_int(key: str, default: int | None = None) -> int | None:
    val = os.environ.get(key)
    if val is None or val == "":
        return default
    return int(val)


def env_float(key: str, default: float | None = None) -> float | None:
    val = os.environ.get(key)
    if val is None or val == "":
        return default
    return float(val)


BASE_DIR = Path(__file__).resolve().parent.parent.parent

# ---------------------------------------------------------------- Core
SECRET_KEY = env("DJANGO_SECRET_KEY", "")
DEBUG = False
ALLOWED_HOSTS: list[str] = [h for h in env("DJANGO_ALLOWED_HOSTS", "").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # "studies" is added here in Phase 2 once the app exists — the project
    # is deliberately runnable (admin, auth) before that lands.
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "dataopt.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "dataopt.wsgi.application"
ASGI_APPLICATION = "dataopt.asgi.application"

# ---------------------------------------------------------------- Database
# PostgreSQL is the authoritative store (decision: no permanent transcript
# files; exports are generated on demand from these tables).
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "data_opt"),
        "USER": env("POSTGRES_USER", "data_opt"),
        "PASSWORD": env("POSTGRES_PASSWORD", ""),
        "HOST": env("POSTGRES_HOST", "localhost"),
        "PORT": env("POSTGRES_PORT", "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Every study/run/answer/download/status view requires sign-in; anonymous
# requests are redirected to the login page rather than shown a 403.
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------- Celery / Redis
# Background processing (Phase 3): one Celery task per Study/PipelineRun.
# Redis doubles as broker and result backend so task status can back the
# status-polling view without extra infrastructure.
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", "redis://localhost:6379/0")
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE

# ---------------------------------------------------------------- core_pipeline
# Filesystem/model paths the old config.py hardcoded per-machine. None of
# these are machine-specific defaults — an empty/relative default keeps the
# repo portable; every real deployment sets these via environment.
EMBEDDING_MODEL_PATH = env("EMBEDDING_MODEL_PATH", str(BASE_DIR / "embeddings_local" / "all-MiniLM-L6-v2"))
HANDBOOK_PDF_PATH = env("HANDBOOK_PDF_PATH", str(BASE_DIR / "dependants" / "Structured EAU1 _student_ handbook (2).pdf"))

# ---------------------------------------------------------------- OCR (Docling)
# Phase 3 wires these into the per-page OCR wrapper; declared here now so the
# setting names are fixed and documented from the start.
OCR_TIMEOUT_SECONDS = env_float("OCR_TIMEOUT_SECONDS", 60.0)

# ---------------------------------------------------------------- OpenAI / GPT-5
# All three LLM call sites (retrieve_guide, generate, formatter) go through
# llm.openai_client.OpenAIResponsesClient, configured only from here — no
# node or core_pipeline module reads these directly.
OPENAI_API_KEY = env("OPENAI_API_KEY", "")
OPENAI_MODEL = env("OPENAI_MODEL", "gpt-5")
OPENAI_MAX_OUTPUT_TOKENS = env_int("OPENAI_MAX_OUTPUT_TOKENS", 1024)
OPENAI_TIMEOUT_SECONDS = env_float("OPENAI_TIMEOUT_SECONDS", 60.0)
OPENAI_MAX_RETRIES = env_int("OPENAI_MAX_RETRIES", 3)

# Optional, per-run cost ceiling. Disabled by default until real benchmark
# data exists — enabling this without OPENAI_COST_LIMIT_USD set is a no-op.
OPENAI_COST_LIMIT_ENABLED = env_bool("OPENAI_COST_LIMIT_ENABLED", False)
OPENAI_COST_LIMIT_USD = env_float("OPENAI_COST_LIMIT_USD", None)
