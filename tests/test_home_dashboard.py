"""Accueil (portefeuilles ouverts, exemple de frais) et tableau de bord
client (resume en statuts publics)."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import Customer
from apps.api.status import customer_overview
from apps.providers.plopplop.client import PaymentIntent
from apps.transactions import services
from apps.transactions.models import Transaction, WalletSetting
from apps.transactions.states import State
from apps.web.labels import format_htg

PHONE = "50937123456"
CODE = "123456"


@pytest.fixture
def client(client):
    client.cookies["django_language"] = "fr"
    return client


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


def _home(client) -> str:
    return client.get(reverse("web:home")).content.decode()


def _band(html: str) -> str:
    start = html.index("data-wallets")
    return html[start : html.index("</ul>", start)]


@pytest.mark.django_db
def test_home_shows_only_wallets_open_in_the_console(client):
    WalletSetting.objects.filter(wallet="kashpaw").update(payment_enabled=False, payout_enabled=False)

    band = _band(_home(client))

    assert 'data-wallet="moncash"' in band and 'data-wallet="natcash"' in band
    assert 'data-wallet="carte"' in band
    assert 'data-wallet="kashpaw"' not in band
    assert "payer et recevoir" in band and "payer seulement" in band


@pytest.mark.django_db
def test_home_example_is_the_real_quote(client):
    html = _home(client)
    real = services.quote_transfer(source_wallet="moncash", destination_wallet="natcash", net_amount=Decimal("1000"))

    example = html[html.index("data-example") :]
    example = example[: example.index("</section>")]
    assert f"{format_htg(real.total_charged)} HTG" in example
    assert "NatCash" in example and "MonCash" in example


@pytest.mark.django_db
def test_home_example_hidden_when_no_route_is_open(client):
    WalletSetting.objects.update(payout_enabled=False)

    html = _home(client)

    assert services.example_quote() is None
    assert "data-example" not in html


@pytest.mark.django_db
def test_home_form_comes_before_the_presentation(client):
    html = _home(client)
    assert html.index('action="/envoyer/"') < html.index("Comment ça marche")


@pytest.mark.django_db
def test_overview_counts_public_statuses_only():
    customer = Customer.objects.create(phone=PHONE)
    rows = [
        (State.AWAITING_PAYMENT, "100"),
        (State.PAYOUT_FAILED, "200"),  # interne, affiche « en cours »
        (State.COMPLETED, "300"),
        (State.COMPLETED, "400"),
        (State.CANCELLED, "500"),
    ]
    for state, amount in rows:
        txn = services.create_transaction(
            source_wallet="moncash", destination_wallet="natcash", recipient_phone="50932123456",
            net_amount=Decimal(amount), customer=customer,
        )
        # Etat pose directement : seul le resume est teste ici, pas le parcours.
        Transaction.objects.filter(pk=txn.pk).update(state=state)

    overview = customer_overview(customer)

    assert overview == {"total": 5, "ongoing": 2, "delivered": 2, "delivered_amount": Decimal("700")}


@pytest.mark.django_db
def test_dashboard_shows_summary_and_wallet_logos(client):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:login"), {"phone": PHONE})
    client.post(reverse("web:login_code"), {"code": CODE})
    customer = Customer.objects.get(phone=PHONE)
    from apps.api import services as api_services

    with mock.patch("apps.transactions.services.get_client") as get_client:
        get_client.return_value.create_payment.return_value = PaymentIntent(
            transaction_id="PAY-1", reference="PP", redirect_url="https://pay.example/abc"
        )
        quote = services.quote_transfer(source_wallet="moncash", destination_wallet="natcash", net_amount=Decimal("1000"))
        api_services.create_transfer(
            customer=customer, idempotency_key="cle-dash", source_wallet="moncash", destination_wallet="natcash",
            recipient_phone="50932123456", net_amount=Decimal("1000"), expected_total=quote.total_charged,
        )

    html = client.get(reverse("web:transfers")).content.decode()

    assert "data-overview" in html
    assert 'alt="MonCash"' in html and 'alt="NatCash"' in html
    assert "status-awaiting_payment" in html
