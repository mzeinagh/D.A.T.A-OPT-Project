"""Guards the split between the web and worker container images.

The web image (requirements-web.txt) ships without PyTorch,
sentence-transformers or Docling. The website still imports the pipeline
modules to queue jobs, so a top-level import of any of those, anywhere in
that chain, would crash the web container on the first page that reaches
it. This test loads the website's code with those packages blocked, in a
fresh interpreter so other tests that did import them don't hide the
problem.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

WORKER_ONLY = ("torch", "torchvision", "sentence_transformers", "transformers", "docling")

SCRIPT = textwrap.dedent(
    f"""
    import importlib.abc
    import os
    import sys

    BLOCKED = {WORKER_ONLY!r}

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(f"worker-only package imported by web code: {{name}}")
            return None

    sys.meta_path.insert(0, Block())
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dataopt.settings.dev")

    import django
    django.setup()

    import dataopt.urls  # every view module the website routes to
    import dataopt.wsgi
    import studies.tasks  # imported by views to queue jobs
    """
)


def test_web_code_loads_without_worker_only_packages():
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
