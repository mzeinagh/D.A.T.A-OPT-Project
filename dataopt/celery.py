"""Celery application. Only one long-running task type is planned per the
locked plan: one task per Study/PipelineRun covering corpus build, indexing,
and the full 11-question graph loop — never one task per question, since
that would defeat the "build the index once, reuse it for every question"
design the pipeline already relies on.

Task modules land in `studies/tasks.py` in Phase 3; this file only wires the
Celery app itself so `dataopt` is importable end-to-end from Phase 0.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dataopt.settings.dev")

app = Celery("dataopt")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
