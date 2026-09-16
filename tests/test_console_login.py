"""Page de connexion a la console.

Regles protegees : rien de la console n'est visible avant identification ;
le message d'echec ne revele pas si le compte existe ; l'ecran demande est
retrouve apres connexion, sans redirection vers un site externe.
"""

from __future__ import annotations

import re

import pytest
from django.urls import reverse

from apps.accounts.models import Role, User

PASSWORD = "Plip-Console-2026!kx"


@pytest.fixture
def operator(db):
    return User.objects.create_user(username="op@plip.ht", password=PASSWORD, role=Role.OPERATOR)


@pytest.mark.django_db
def test_login_page_shows_no_console_navigation(client):
    html = client.get(reverse("console:login")).content.decode()

    for url in (reverse("console:dashboard"), reverse("console:queue"), reverse("console:treasury")):
        assert f'href="{url}"' not in html, url
    assert "AnonymousUser" not in html


@pytest.mark.django_db
def test_login_page_is_centred_and_self_contained(client):
    html = client.get(reverse("console:login")).content.decode()

    assert "<!doctype html>" in html.lower()
    body = re.search(r"<body[^>]*>", html).group(0)
    main = re.search(r"<main[^>]*>", html).group(0)
    assert "min-h-screen" in body and "min-h-screen" in main
    assert "justify-center" in main and "mx-auto" in main and "max-w-sm" in main
    assert 'name="viewport"' in html and "noindex" in html


@pytest.mark.django_db
def test_form_labels_match_the_account_and_help_the_browser(client):
    html = client.get(reverse("console:login")).content.decode()

    assert "Identifiant" in html and "Mot de passe" in html
    assert 'autocomplete="username"' in html and 'autocomplete="current-password"' in html
    assert "autofocus" in html and 'type="password"' in html


@pytest.mark.django_db
def test_fields_are_styled_without_javascript(client):
    html = client.get(reverse("console:login")).content.decode()

    inputs = re.findall(r"<input[^>]*>", html)
    visible = [i for i in inputs if 'type="hidden"' not in i]
    assert len(visible) == 2
    assert all('class="w-full rounded border' in i for i in visible)
    # Seul script de la page : la CDN Tailwind. Aucun script maison
    # n'habille les champs apres coup.
    assert re.findall(r"<script[^>]*>", html) == ['<script src="https://cdn.tailwindcss.com">']


@pytest.mark.django_db
@pytest.mark.parametrize("username, password", [("op@plip.ht", "faux-mot-de-passe"), ("inconnu@plip.ht", PASSWORD)])
def test_failed_login_says_the_same_thing_whether_the_account_exists(client, operator, username, password):
    response = client.post(reverse("console:login"), {"username": username, "password": password})
    html = response.content.decode()

    assert response.status_code == 200
    assert "Identifiant ou mot de passe incorrect." in html
    assert username not in html.split("<form")[0]  # rien qui confirme le compte
    assert password not in html


@pytest.mark.django_db
def test_login_returns_to_the_requested_screen(client, operator):
    queue = reverse("console:queue")

    redirected = client.get(queue)
    assert redirected["Location"] == f"{reverse('console:login')}?next={queue}"

    response = client.post(f"{reverse('console:login')}?next={queue}", {"username": "op@plip.ht", "password": PASSWORD})
    assert response["Location"] == queue
    assert client.get(queue).status_code == 200


@pytest.mark.django_db
def test_login_never_redirects_to_another_site(client, operator):
    response = client.post(
        f"{reverse('console:login')}?next=https://exemple-malveillant.test/",
        {"username": "op@plip.ht", "password": PASSWORD},
    )
    assert response["Location"] == reverse("console:dashboard")


@pytest.mark.django_db
def test_already_connected_goes_straight_to_the_dashboard(client, operator):
    client.force_login(operator)
    assert client.get(reverse("console:login"))["Location"] == reverse("console:dashboard")


@pytest.mark.django_db
def test_disabled_account_cannot_connect(client, operator):
    User.objects.filter(pk=operator.pk).update(is_active=False)

    response = client.post(reverse("console:login"), {"username": "op@plip.ht", "password": PASSWORD})

    assert response.status_code == 200
    assert client.get(reverse("console:dashboard")).status_code == 302
