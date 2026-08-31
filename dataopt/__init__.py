# Makes `from dataopt.celery import app as celery_app` importable as
# `dataopt.celery_app`, which is how Celery expects the app to be
# discoverable once `studies/tasks.py` exists (Phase 3).
from .celery import app as celery_app  # noqa: E402,F401

__all__ = ("celery_app",)
