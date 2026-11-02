"""The "Create account" page (open sign-up for the test environment)."""
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

User = get_user_model()

pytestmark = pytest.mark.django_db

GOOD = {
    "first_name": "Sam",
    "last_name": "Lee",
    "username": "slee",
    "password1": "Tox-study-2026!",
    "password2": "Tox-study-2026!",
}


def test_signup_creates_account_signs_in_and_lands_on_studies(client):
    resp = client.post(reverse("signup"), GOOD)

    assert resp.status_code == 302
    assert resp.url == reverse("studies:study_list")
    user = User.objects.get(username="slee")
    assert (user.first_name, user.last_name) == ("Sam", "Lee")
    assert not user.is_staff and not user.is_superuser
    assert client.get(reverse("studies:study_list")).status_code == 200


def test_mismatched_or_weak_passwords_are_rejected(client):
    bad = {**GOOD, "password2": "something-else"}
    assert client.post(reverse("signup"), bad).status_code == 200
    weak = {**GOOD, "password1": "12345678", "password2": "12345678"}
    assert client.post(reverse("signup"), weak).status_code == 200
    assert not User.objects.filter(username="slee").exists()


def test_duplicate_username_is_rejected(client):
    User.objects.create_user(username="slee", password="x")

    resp = client.post(reverse("signup"), GOOD)
    assert resp.status_code == 200
    assert "already exists" in resp.content.decode()
    assert User.objects.count() == 1


def test_form_has_no_email_field(client):
    assert 'name="email"' not in client.get(reverse("signup")).content.decode()


def test_signed_in_user_is_sent_to_studies(client):
    User.objects.create_user(username="aida", password="pw12345")
    client.login(username="aida", password="pw12345")
    resp = client.get(reverse("signup"))
    assert resp.status_code == 302


def test_links_to_signup_from_login_and_welcome(client):
    signup = reverse("signup")
    assert f'href="{signup}"' in client.get(reverse("login")).content.decode()
    assert f'href="{signup}"' in client.get("/").content.decode()


def test_signup_can_be_switched_off(client, settings):
    settings.ALLOW_SIGNUP = False
    assert client.get(reverse("signup")).status_code == 404
    assert client.post(reverse("signup"), GOOD).status_code == 404
    assert "Create account" not in client.get(reverse("login")).content.decode()
    assert not User.objects.exists()
