"""Controle d'admission : refuser plutot que retenir l'argent du client.

Regles protegees : on refuse a la CREATION et jamais apres ; un transfert
deja encaisse n'est jamais bloque ; la console n'est pas plafonnee ; le
client n'apprend jamais la longueur de la file ; chaque refus laisse une
trace ; et l'interrupteur rouvre tout immediatement.
"""

from __future__ import annotations

import re
from html import unescape
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services as accounts
from apps.accounts.models import AuditLog, Customer
from apps.api import services as api_services
from apps.ledger import services as ledger
from apps.transactions import admission, services
from apps.transactions.models import Transaction, TransactionEvent, Wallet
from apps.transactions.states import State

CODE = "123456"
PHONE = "50937123456"
GET_CLIENT = "apps.transactions.services.get_client"
COOLDOWN = 125


@pytest.fixture(autouse=True)
def setup(db, settings):
    ledger.ensure_accounts()
    settings.PAYOUT_COOLDOWN_SECONDS = COOLDOWN
    settings.ADMISSION_CONTROL_ENABLED = True
    # 10 places : la 11e creation est refusee.
    settings.ADMISSION_MAX_WAIT_SECONDS = 10 * COOLDOWN
    settings.PAYOUT_STALL_SECONDS = 3 * COOLDOWN
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def customer(db):
    return Customer.objects.create(phone=PHONE, language="fr")


def _create(customer=None, amount="1000"):
    return services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone="50932123456",
        net_amount=Decimal(amount),
        customer=customer,
    )


def _queued(customer=None):
    txn = _create(customer)
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    return Transaction.objects.get(pk=txn.pk)


def _fill_queue(n, customer=None):
    return [_queued(customer) for _ in range(n)]


def _age_queue(seconds):
    """Recule l'entree en file : le worker parait a l'arret."""
    moment = timezone.now() - timedelta(seconds=seconds)
    TransactionEvent.objects.filter(to_state=State.PAYOUT_QUEUED).update(created_at=moment)
    cache.clear()


# ----------------------------------------------------------------------
# Le seuil
# ----------------------------------------------------------------------
def test_the_queue_accepts_up_to_the_threshold(customer):
    _fill_queue(9)

    txn = _create(customer)  # le dixieme passe

    assert txn.pk is not None
    assert admission.admission_state().accepting is True


def test_one_transfer_too_many_is_refused(customer):
    _fill_queue(10)
    before = Transaction.objects.count()

    with pytest.raises(services.ServiceSaturated) as raised:
        _create(customer)

    assert raised.value.saturation.reason == admission.SATURATED
    assert raised.value.retry_after_seconds == COOLDOWN
    assert Transaction.objects.count() == before  # rien n'a ete ecrit


def test_a_console_transaction_is_never_refused():
    """Un operateur doit pouvoir rattraper la situation qu'il traite."""
    _fill_queue(20)

    txn = _create(customer=None)

    assert txn.pk is not None


def test_the_switch_reopens_everything(customer, settings):
    _fill_queue(20)
    settings.ADMISSION_CONTROL_ENABLED = False

    assert _create(customer).pk is not None


def test_free_slots_counts_down_as_the_queue_fills(customer):
    assert admission.admission_state().free_slots == 10
    _fill_queue(4)
    cache.clear()
    assert admission.admission_state().free_slots == 6


# ----------------------------------------------------------------------
# Worker a l'arret
# ----------------------------------------------------------------------
def test_a_stopped_payout_worker_closes_the_door(customer):
    _fill_queue(2)  # file courte : seul l'arret peut fermer
    _age_queue(10 * COOLDOWN)

    with pytest.raises(services.ServiceSaturated) as raised:
        _create(customer)

    assert raised.value.saturation.reason == admission.STALLED
    assert raised.value.retry_after_seconds == 60


def test_the_door_reopens_when_the_worker_starts_again(customer):
    queued = _fill_queue(2)
    _age_queue(10 * COOLDOWN)
    with pytest.raises(services.ServiceSaturated):
        _create(customer)

    queued[0].transition(State.PAYOUT_IN_FLIGHT)  # le worker repart
    cache.clear()

    assert _create(customer).pk is not None


# ----------------------------------------------------------------------
# Jamais apres l'encaissement
# ----------------------------------------------------------------------
def test_a_collected_transfer_is_never_blocked_by_saturation(customer):
    """Une transaction encaissee est une dette : elle se decaisse."""
    txn = _queued(customer)
    _fill_queue(30)  # la file explose apres coup

    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.return_value = mock.Mock(
            succeeded=True, fee=Decimal("40"), transaction_id="PP-1", api_reference="", balance_after=None
        )
        services.execute_payout(Transaction.objects.get(pk=txn.pk))

    assert Transaction.objects.get(pk=txn.pk).state == State.COMPLETED


def test_a_quote_is_never_refused_by_saturation():
    _fill_queue(20)

    quote = services.quote_transfer(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH, net_amount=Decimal("1000")
    )

    assert quote.total_charged > 0


# ----------------------------------------------------------------------
# Trace
# ----------------------------------------------------------------------
def test_every_refusal_is_audited(customer):
    """La trace doit survivre a l'annulation de la transaction de base."""
    _fill_queue(10)
    quote = services.quote_transfer(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH, net_amount=Decimal("1000")
    )

    with pytest.raises(services.ServiceSaturated):
        api_services.create_transfer(
            customer=customer,
            idempotency_key="admission-refus-0001",
            source_wallet="moncash",
            destination_wallet="natcash",
            recipient_phone="50932123456",
            net_amount=Decimal("1000"),
            expected_total=quote.total_charged,
        )

    log = AuditLog.objects.get(action="admission.refused")
    assert (log.user, log.target, log.allowed) == (None, PHONE, False)
    assert log.detail["reason"] == admission.SATURATED
    assert log.detail["depth"] == 10


# ----------------------------------------------------------------------
# Ce que voit le client
# ----------------------------------------------------------------------
def _api():
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        accounts.request_code(PHONE)
    customer, raw, _ = accounts.verify_code(PHONE, CODE)
    from rest_framework.test import APIClient

    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return api, customer


def test_the_api_says_to_wait_without_revealing_the_queue():
    api, customer = _api()
    _fill_queue(10)
    quote = services.quote_transfer(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH, net_amount=Decimal("1000")
    )

    response = api.post(
        reverse("api:transfers"),
        {
            "source_wallet": "moncash",
            "destination_wallet": "natcash",
            "recipient_phone": "50932123456",
            "net_amount": "1000",
            "expected_total": str(quote.total_charged),
        },
        format="json",
        HTTP_IDEMPOTENCY_KEY="admission-api-0001",
    )
    body = response.json()

    assert response.status_code == 503
    assert body["error"]["code"] == "SERVICE_SATURATED"
    assert body["error"]["retry_after"] == COOLDOWN
    # Ni profondeur, ni rang, ni attente projetee.
    assert "depth" not in str(body) and "10" not in body["error"]["message"]


def _web_login(client):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:login"), {"phone": PHONE})
    client.post(reverse("web:login_code"), {"code": CODE})
    return Customer.objects.get(phone=PHONE)


def test_the_home_page_warns_before_the_customer_fills_the_form(client):
    assert 'data-banner="saturated"' not in client.get(reverse("web:home")).content.decode()

    _fill_queue(10)
    cache.clear()
    html = client.get(reverse("web:home")).content.decode()

    assert 'data-banner="saturated"' in html
    text = re.sub(r"<[^>]+>", " ", html)
    assert "beaucoup de transferts" in text
    # Jamais un chiffre de file dans le message.
    assert not re.search(r"\b10\b|\bfile\b|\brang\b", text.lower())


def test_the_web_refuses_the_creation_and_explains(client):
    customer = _web_login(client)
    _fill_queue(10)
    quote = services.quote_transfer(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH, net_amount=Decimal("1000")
    )
    draft = {
        "source_wallet": "moncash",
        "destination_wallet": "natcash",
        "recipient_phone": "50932123456",
        "net_amount": "1000",
    }
    session = client.session
    session["web:draft"] = draft
    session["web:pending"] = {"cle-1": draft}
    session.save()

    response = client.post(
        reverse("web:confirm"), {"idempotency_key": "cle-1", "expected_total": str(quote.total_charged)}
    )
    html = client.get(response["Location"]).content.decode()

    assert "beaucoup de transferts" in re.sub(r"<[^>]+>", " ", html)
    assert not Transaction.objects.filter(customer=customer, state=State.CREATED).exists()


#: Chaine source, telle qu'elle est ecrite dans la vue.
SATURATED_FR = (
    "Nous recevons beaucoup de transferts en ce moment. Plutôt que de prendre votre argent "
    "sans pouvoir le livrer rapidement, nous préférons attendre. Réessayez dans quelques minutes."
)


@pytest.mark.parametrize("language", ["ht", "en"])
def test_the_message_is_translated(client, language):
    """Le texte affiche vient bien du catalogue, pas du francais.

    On ne code pas la traduction ici : le catalogue en est la source, et
    sa completude est garantie par le garde-fou de tests/test_web.py.
    """
    from django.utils import translation

    _fill_queue(10)
    cache.clear()
    client.cookies["django_language"] = language

    html = client.get(reverse("web:home")).content.decode()
    with translation.override(language):
        expected = translation.gettext(SATURATED_FR)

    # unescape : Django echappe les apostrophes des textes traduits.
    text = unescape(re.sub(r"<[^>]+>", " ", html))

    assert expected != SATURATED_FR, "chaine non traduite dans le catalogue"
    assert expected in text


# ----------------------------------------------------------------------
# Reouverture
# ----------------------------------------------------------------------
def test_the_reopening_time_is_one_cooldown_per_missing_slot():
    _fill_queue(13)  # 3 de trop
    now = timezone.now()

    state = admission.admission_state(now=now)
    reopens = admission.reopens_at(state, now=now)

    assert state.reason == admission.SATURATED
    assert reopens == now + timedelta(seconds=4 * COOLDOWN)


def test_a_stopped_worker_has_no_reopening_time():
    _fill_queue(2)
    _age_queue(10 * COOLDOWN)

    state = admission.admission_state()

    assert (state.reason, admission.reopens_at(state)) == (admission.STALLED, None)
