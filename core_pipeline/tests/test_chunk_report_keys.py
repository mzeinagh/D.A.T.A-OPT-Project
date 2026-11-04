"""Regression test for chunk_report's page-score lookups.

chunk_report stores one dict of scores per page and later reads the scores
of neighbouring pages inside try/except blocks. A misspelled key there
(e.g. 'study_prface_score') raised KeyError, which the bare except swallowed,
so summary and introduction pages silently lost their neighbour bonus.
This test fails if any lookup names a key the score dict never defines.
"""
import inspect
import re

from core_pipeline import utils

DEFINED_KEYS = {
    "toc_score",
    "title_page_score",
    "study_preface_score",
    "pdf_page_num",
    "study_id",
    "summary_score",
    "intro_score",
}


def test_every_score_lookup_uses_a_defined_key():
    source = inspect.getsource(utils.chunk_report)
    used = set(re.findall(r"\bl?value\['([a-z_]+)'\]", source))
    assert used, "expected chunk_report to read page scores"
    assert used <= DEFINED_KEYS, f"unknown score keys: {sorted(used - DEFINED_KEYS)}"
