# Two images from one file, always built from the same commit by
# scripts/azure/deploy.sh:
#   docker build --target web .     the website (small: no PyTorch, no OCR)
#   docker build --target worker .  the Celery worker (OCR, embeddings, models)
# Both share the "base" and "web-deps" layers, so the registry stores those once.

# ---------------------------------------------------------------- base
FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# uid/gid 1000 match the Azure Files mount options (uid=1000,gid=1000),
# so the app user owns the uploads on the shared file share.
RUN useradd --uid 1000 --create-home app
WORKDIR /app

# ---------------------------------------------------------------- web-deps
FROM base AS web-deps
COPY requirements-web.txt .
RUN pip install -r requirements-web.txt

# ---------------------------------------------------------------- web
FROM web-deps AS web
COPY --chown=app:app . .
# Collect the admin's CSS/JS for WhiteNoise. prod settings refuse to load
# without a secret key and allowed hosts, so this one build command gets
# throwaway values; the real ones come from Container Apps at runtime.
RUN DJANGO_SETTINGS_MODULE=dataopt.settings.prod \
    DJANGO_SECRET_KEY=collectstatic-only \
    DJANGO_ALLOWED_HOSTS=localhost \
    python manage.py collectstatic --noinput
USER app
ENV DJANGO_SETTINGS_MODULE=dataopt.settings.prod
EXPOSE 8000
CMD ["gunicorn", "dataopt.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "--access-logfile", "-"]

# ---------------------------------------------------------------- worker
FROM web-deps AS worker
ENV HF_HOME=/opt/models/hf
# libgl1 and libglib2.0-0: OpenCV, which Docling uses, fails without them
# (docs/LINUX_TEST_DEPLOYMENT.md 12.2).
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*
# CPU-only PyTorch first, from PyTorch's own index. From the default index
# pip installs the GPU build plus ~20 NVIDIA packages, several GB more.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision
COPY requirements-worker.txt .
RUN pip install -r requirements-worker.txt
# Download the models now, so the worker never downloads at startup:
# the embedding model (its weights are not in git; the other formats on
# the model page are skipped) and the models Docling uses, by converting
# page 1 of the bundled handbook exactly as ocr_docling() does.
COPY embeddings_local/ embeddings_local/
COPY dependants/eau1-handbook-structured.pdf /tmp/warmup.pdf
RUN python -c "from huggingface_hub import snapshot_download; \
snapshot_download('sentence-transformers/all-MiniLM-L6-v2', \
local_dir='embeddings_local/all-MiniLM-L6-v2', \
ignore_patterns=['onnx/*', 'openvino/*', '*.h5', '*.msgpack', '*.ot'])" \
 && python -c "from docling.document_converter import DocumentConverter; \
DocumentConverter().convert('/tmp/warmup.pdf', page_range=(1, 1))" \
 && rm /tmp/warmup.pdf \
 && mkdir -p /opt/models \
 && chown -R app:app /opt/models embeddings_local
COPY --chown=app:app . .
USER app
ENV DJANGO_SETTINGS_MODULE=dataopt.settings.prod
CMD ["celery", "-A", "dataopt", "worker", "--loglevel=info", "--concurrency=1"]
