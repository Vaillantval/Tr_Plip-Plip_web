"""URL de retour plopplop : https://plip.ht/paiement/retour/

Regles protegees : le retour ne change aucun etat et n'appelle pas
plopplop (n'importe qui peut ouvrir l'adresse) ; il n'ouvre jamais le
transfert d'un autre client ; il fonctionne quels que soient les
parametres renvoyes, y compris aucun, et apres reconnexion.
"""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import Customer
from apps.providers.plopplop.client import PaymentIntent
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

GET_CLIENT = "apps.transactions.services.get_client"
CODE = "123456"
PHONE = "50937123456"
OTHER = "50948123456"


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


def _login(client, next_url=None):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:login"), {"phone": PHONE, **({"next": next_url} if next_url else {})})
    query = f"?{urlencode({'next': next_url})}" if next_url else ""
    return client.post(reverse("web:login_code") + query, {"code": CODE})


def _awaiting(phone=PHONE, provider_id="PAY-1"):
    customer, _ = Customer.objects.get_or_create(phone=phone)
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH,
        recipient_phone="50932123456", net_amount=Decimal("1000"), customer=customer,
    )
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.create_payment.return_value = PaymentIntent(
            transaction_id=provider_id, reference=txn.reference, redirect_url="https://pay.example/x"
        )
        services.start_payment(txn)
    txn.refresh_from_db()
    return txn


def test_return_url_path():
    assert reverse("web:payment_return") == "/paiement/retour/"


@pytest.mark.django_db
@pytest.mark.parametrize("param", ["refference_id", "reference", "transaction_id", "id_transaction"])
def test_return_opens_the_customers_own_transfer_whatever_the_parameter(client, param):
    txn = _awaiting(provider_id="173763124912345")
    _login(client)
    value = txn.payment_provider_id if param.endswith("transaction_id") or param == "transaction_id" else txn.reference

    with mock.patch(GET_CLIENT) as get_client:
        response = client.get(reverse("web:payment_return"), {param: value})

    assert response["Location"] == reverse("web:transfer_detail", args=[txn.reference])
    get_client.assert_not_called()  # le retour ne prouve rien et n'appelle pas plopplop
    txn.refresh_from_db()
    assert txn.state == State.AWAITING_PAYMENT


@pytest.mark.django_db
def test_return_without_parameters_opens_the_latest_live_transfer(client):
    older = _awaiting(provider_id="P1")
    latest = _awaiting(provider_id="P2")
    _login(client)

    assert client.get(reverse("web:payment_return"))["Location"] == reverse("web:transfer_detail", args=[latest.reference])

    for txn in (older, latest):
        txn.transition(State.CANCELLED)
    assert client.get(reverse("web:payment_return"))["Location"] == reverse("web:transfers")


@pytest.mark.django_db
def test_return_never_opens_another_customers_transfer(client):
    foreign = _awaiting(phone=OTHER, provider_id="FOREIGN")
    _login(client)

    for params in ({"refference_id": foreign.reference}, {"transaction_id": "FOREIGN"}):
        location = client.get(reverse("web:payment_return"), params)["Location"]
        assert foreign.reference not in location
        assert location == reverse("web:transfers")


@pytest.mark.django_db
def test_return_after_session_expiry_goes_through_login_then_to_the_transfer(client):
    txn = _awaiting()

    response = client.get(reverse("web:payment_return"), {"refference_id": txn.reference})
    assert response["Location"].startswith(reverse("web:login"))
    next_url = parse_qs(urlsplit(response["Location"]).query)["next"][0]

    assert _login(client, next_url=next_url)["Location"] == next_url
    assert client.get(next_url)["Location"] == reverse("web:transfer_detail", args=[txn.reference])
    assert Transaction.objects.get(pk=txn.pk).state == State.AWAITING_PAYMENT
