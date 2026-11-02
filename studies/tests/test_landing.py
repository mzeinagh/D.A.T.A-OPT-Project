"""The welcome page at "/" and the path from it into the app."""
import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

User = get_user_model()

pytestmark = pytest.mark.django_db


def test_welcome_page_is_public_and_points_to_login(client):
    resp = client.get("/")

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "Upload your study. Get structured answers." in html
    assert f'href="{reverse("login")}"' in html
    assert "Get started" in html


def test_signed_in_user_sees_link_to_studies(client):
    User.objects.create_user(username="aida", password="pw12345")
    client.login(username="aida", password="pw12345")

    html = client.get("/").content.decode()

    assert "Open your studies" in html
    assert f'href="{reverse("studies:study_list")}"' in html


def test_login_lands_on_studies(client):
    User.objects.create_user(username="aida", password="pw12345")

    resp = client.post(reverse("login"), {"username": "aida", "password": "pw12345"})

    assert resp.status_code == 302
    assert resp.url == reverse("studies:study_list")


def test_login_still_returns_to_requested_page(client):
    User.objects.create_user(username="aida", password="pw12345")
    upload_url = reverse("studies:upload")

    resp = client.post(reverse("login"), {"username": "aida", "password": "pw12345", "next": upload_url})

    assert resp.url == upload_url


def test_app_pages_still_require_login(client):
    resp = client.get(reverse("studies:study_list"))

    assert resp.status_code == 302
    assert resp.url.startswith(reverse("login"))


def test_welcome_page_spells_out_the_name_and_unit(client):
    html = client.get("/").content.decode()

    assert "Document-based Automated Toxicity Analyzer" in html
    assert "Environmental Assessment Unit" in html
    assert "flexible, scalable design" in html
    assert "Intelligent scientific document analysis" not in html


def test_brand_links_to_welcome_page_on_every_page(client):
    User.objects.create_user(username="aida", password="pw12345")
    client.login(username="aida", password="pw12345")

    for url in ["/", reverse("studies:study_list"), reverse("studies:upload")]:
        html = client.get(url).content.decode()
        assert f'class="brand" href="{reverse("home")}"' in html
