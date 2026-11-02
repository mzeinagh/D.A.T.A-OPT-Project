"""Names and times as people see them: names capitalized without
changing what's stored, and times in Ottawa time with EST/EDT handled."""
import datetime

import pytest
from django.contrib.auth import get_user_model

from studies.templatetags.display import display_name, when

User = get_user_model()


@pytest.mark.django_db
def test_username_is_capitalized_but_not_changed():
    user = User.objects.create_user(username="aida", password="x")
    assert display_name(user) == "Aida"
    user.refresh_from_db()
    assert user.username == "aida"


@pytest.mark.django_db
def test_dotted_username_becomes_first_and_last_name():
    assert display_name(User.objects.create_user(username="john.smith", password="x")) == "John Smith"


@pytest.mark.django_db
def test_full_name_is_preferred_and_capitalized():
    user = User.objects.create_user(username="am", password="x", first_name="aida", last_name="motamedi")
    assert display_name(user) == "Aida Motamedi"


def test_winter_time_is_est():
    # 18:52 UTC on Nov 2 2026 is 1:52 PM in Ottawa (EST, UTC-5)
    value = datetime.datetime(2026, 11, 2, 18, 52, tzinfo=datetime.timezone.utc)
    assert when(value) == "Nov 2, 2026, 1:52 PM EST"


def test_summer_time_is_edt():
    # 17:52 UTC on Jul 2 2026 is 1:52 PM in Ottawa (EDT, UTC-4)
    value = datetime.datetime(2026, 7, 2, 17, 52, tzinfo=datetime.timezone.utc)
    assert when(value) == "Jul 2, 2026, 1:52 PM EDT"


def test_iso_string_from_failure_history():
    assert when("2026-11-02T18:52:00+00:00") == "Nov 2, 2026, 1:52 PM EST"


def test_empty_value():
    assert when(None) == "—"
