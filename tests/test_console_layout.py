"""Gabarit de la console : menu, pastilles, recherche, logo.

Regles protegees : chaque ecran reste atteignable depuis le menu ; une
pastille ne s'allume que pour ce qui attend un humain ; la recherche
n'ouvre que ce qui correspond exactement.
"""

from __future__ import annotations

import re
from decimal import Decimal
from unittest import mock

import pytest
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.ledger import services as ledger
from apps.providers.plopplop import exceptions as pp
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

GET_CLIENT = "apps.transactions.services.get_client"
SCREENS = ("dashboard", "queue", "transactions", "exceptions", "claims", "treasury", "methods")


@pytest.fixture
def books(db):
    ledger.ensure_accounts()


@pytest.fixture
def operator(db):
    return User.objects.create_user(username="operateur", password="x", role=Role.OPERATOR)


def _queued_txn() -> Transaction:
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone="50932345678",
        sender_phone="50937000000",
        net_amount=Decimal("1000"),
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()
    return txn


def _failed_txn() -> Transaction:
    txn = _queued_txn()
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.side_effect = pp.InsufficientBalance(
            "Solde epuise", code="INSUFFICIENT_BALANCE", status=400
        )
        services.execute_payout(txn)
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_FAILED
    return txn


def _badge(html: str, kind: str) -> str | None:
    found = re.search(r'data-nav-badge="' + kind + r'"[^>]*>(\d+)<', html)
    return found.group(1) if found else None


@pytest.mark.django_db
def test_every_screen_is_in_the_menu_and_the_current_one_is_marked(client, operator):
    client.force_login(operator)
    for screen in SCREENS:
        html = client.get(reverse(f"console:{screen}")).content.decode()
        for target in SCREENS:
            assert f'href="{reverse(f"console:{target}")}"' in html, (screen, target)
        current = re.search(r'href="([^"]+)"\s+class="[^"]*"\s+aria-current="page"', html)
        assert current and current.group(1) == reverse(f"console:{screen}"), screen


@pytest.mark.django_db
def test_no_badge_when_nothing_waits(client, operator, books):
    _queued_txn()  # en file : normal, rien a traiter
    client.force_login(operator)
    html = client.get(reverse("console:dashboard")).content.decode()
    assert _badge(html, "exceptions") is None and _badge(html, "claims") is None


@pytest.mark.django_db
def test_a_failed_payout_lights_the_exceptions_badge(client, operator, books):
    _failed_txn()
    client.force_login(operator)
    html = client.get(reverse("console:dashboard")).content.decode()
    assert _badge(html, "exceptions") == "1"


@pytest.mark.django_db
def test_search_by_reference_opens_the_transfer(client, operator, books):
    txn = _queued_txn()
    client.force_login(operator)
    response = client.get(reverse("console:transactions"), {"q": f" {txn.reference.lower()} "})
    assert response.status_code == 302
    assert response.url == reverse("console:transaction_detail", args=[txn.reference])


@pytest.mark.django_db
def test_search_by_phone_lists_that_customer(client, operator):
    client.force_login(operator)
    response = client.get(reverse("console:transactions"), {"q": "3700 0000"})
    assert response.status_code == 302
    assert response.url == reverse("console:transactions") + "?customer=50937000000"


@pytest.mark.django_db
def test_search_with_nothing_matching_says_so(client, operator):
    client.force_login(operator)
    response = client.get(reverse("console:transactions"), {"q": "ZZZ"})
    assert response.status_code == 200
    assert "Aucun transfert ne correspond" in response.content.decode()


@pytest.mark.django_db
def test_logo_is_served_and_used(client, operator):
    from django.contrib.staticfiles import finders

    assert finders.find("console/logo.svg")
    assert "console/logo.svg" in client.get(reverse("console:login")).content.decode()
    client.force_login(operator)
    html = client.get(reverse("console:dashboard")).content.decode()
    assert 'rel="icon"' in html and "console/logo.svg" in html


@pytest.mark.django_db
def test_environment_badge_only_when_railway_names_it(client, operator, monkeypatch):
    client.force_login(operator)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    assert "data-environment" not in client.get(reverse("console:dashboard")).content.decode()
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "production")
    html = client.get(reverse("console:dashboard")).content.decode()
    assert "data-environment" in html and "Production" in html
