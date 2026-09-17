"""Plafonds cumules par client, sur fenetres glissantes.

Regles protegees : verifies a la CREATION et jamais apres ; seules les
transactions reellement encaissees (ou en attente de paiement) comptent ;
un remboursement libere ; la borne est inclusive ; le refus est audite ;
le client apprend quand il pourra reessayer, sans qu'on lui revele
l'heure exacte de ses envois passes.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import services as accounts
from apps.accounts.models import AuditLog, Customer, Role, User
from apps.ledger import services as ledger
from apps.providers.plopplop.client import PaymentIntent
from apps.transactions import limits, services
from apps.transactions.models import Transaction, TransferLimitPolicy, Wallet
from apps.transactions.states import State

CODE = "123456"
PHONE = "50937123456"
GET_CLIENT = "apps.transactions.services.get_client"
CAP_DAY = Decimal("50000")
CAP_MONTH = Decimal("200000")


@pytest.fixture(autouse=True)
def books(db):
    ledger.ensure_accounts()
    TransferLimitPolicy.objects.update_or_create(pk=1, defaults={"daily_cap": CAP_DAY, "monthly_cap": CAP_MONTH})
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def customer(db):
    return Customer.objects.create(phone=PHONE)


def _create(customer, amount, **kwargs):
    return services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone="50932123456",
        net_amount=Decimal(amount),
        customer=customer,
        **kwargs,
    )


def _collected(customer, amount, *, ago_seconds=0):
    """Transfert encaisse il y a `ago_seconds`."""
    txn = _create(customer, amount)
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    if ago_seconds:
        moment = timezone.now() - timedelta(seconds=ago_seconds)
        Transaction.objects.filter(pk=txn.pk).update(payment_confirmed_at=moment)
    return Transaction.objects.get(pk=txn.pk)


def _used(customer, window=limits.DAY):
    return {w.name: w.used for w in limits.customer_consumption(customer)}[window]


# ----------------------------------------------------------------------
# La borne
# ----------------------------------------------------------------------
def test_a_transfer_exactly_at_the_cap_is_accepted(customer):
    _collected(customer, "40000")

    txn = _create(customer, "10000")

    assert txn.net_amount == Decimal("10000")


def test_one_gourde_over_the_cap_is_refused(customer):
    _collected(customer, "40000")
    before = Transaction.objects.count()

    with pytest.raises(services.LimitExceeded) as raised:
        _create(customer, "10000.01")

    assert raised.value.window.name == limits.DAY
    assert Transaction.objects.count() == before  # rien n'a ete ecrit


def test_the_monthly_cap_refuses_below_the_daily_cap(customer):
    for day in range(1, 5):
        _collected(customer, "50000", ago_seconds=day * 24 * 3600)

    with pytest.raises(services.LimitExceeded) as raised:
        _create(customer, "1000")

    assert raised.value.window.name == limits.MONTH


def test_a_console_transaction_is_never_limited():
    """Un operateur n'est pas un client : pas de plafond sans client."""
    for _ in range(3):
        txn = services.create_transaction(
            source_wallet=Wallet.MONCASH,
            destination_wallet=Wallet.NATCASH,
            recipient_phone="50932123456",
            net_amount=Decimal("50000"),
        )
        txn.transition(State.AWAITING_PAYMENT)
        services.confirm_payment(txn, provider_amount=txn.total_charged)


# ----------------------------------------------------------------------
# Ce qui compte, ce qui ne compte pas
# ----------------------------------------------------------------------
def test_a_quote_never_consumes_the_allowance(customer):
    for _ in range(5):
        services.quote_transfer(
            source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH, net_amount=CAP_DAY
        )

    assert _used(customer) == Decimal("0")
    _create(customer, CAP_DAY)  # le plafond est intact


@pytest.mark.parametrize("final_state", [State.PAYMENT_EXPIRED, State.CANCELLED])
def test_a_payment_that_never_arrived_frees_the_allowance(customer, final_state):
    txn = _create(customer, "50000")
    if final_state == State.PAYMENT_EXPIRED:
        txn.transition(State.AWAITING_PAYMENT)
    txn.transition(final_state)

    assert _used(customer) == Decimal("0")


def test_an_unpaid_transfer_reserves_the_allowance_until_it_expires(customer, settings):
    """Sinon : dix transferts crees sans etre payes, puis tous payes."""
    txn = _create(customer, "50000")
    txn.transition(State.AWAITING_PAYMENT)
    Transaction.objects.filter(pk=txn.pk).update(payment_expires_at=timezone.now() + timedelta(minutes=30))

    assert _used(customer) == Decimal("50000")
    with pytest.raises(services.LimitExceeded):
        _create(customer, "1000")

    # Delai de paiement passe : la reservation tombe.
    Transaction.objects.filter(pk=txn.pk).update(payment_expires_at=timezone.now() - timedelta(seconds=1))
    assert _used(customer) == Decimal("0")


def test_a_refund_frees_the_allowance_again(customer):
    txn = _collected(customer, "50000")
    assert _used(customer) == Decimal("50000")

    services.refund(txn, reason="Test", transfer_reference="MANUEL-1")

    assert _used(customer) == Decimal("0")
    _create(customer, CAP_DAY)  # le plafond plein est de nouveau disponible


def test_the_window_is_rolling_not_calendar(customer):
    _collected(customer, "30000", ago_seconds=25 * 3600)  # hors fenetre
    _collected(customer, "20000", ago_seconds=2 * 3600)  # dans la fenetre

    assert _used(customer) == Decimal("20000")
    _create(customer, "30000")


# ----------------------------------------------------------------------
# Quand le plafond se libere
# ----------------------------------------------------------------------
def test_the_release_date_is_that_of_the_oldest_counted_transfer(customer):
    oldest = _collected(customer, "20000", ago_seconds=23 * 3600)
    _collected(customer, "20000", ago_seconds=1 * 3600)

    with pytest.raises(services.LimitExceeded) as raised:
        _create(customer, "20000")

    expected = oldest.payment_confirmed_at + timedelta(hours=24)
    assert abs((raised.value.frees_at - expected).total_seconds()) < 2


def test_an_amount_above_the_cap_itself_has_no_release_date(customer, settings):
    settings.PRICING = {**settings.PRICING, "MAX_NET_AMOUNT": Decimal("60000")}

    with pytest.raises(services.LimitExceeded) as raised:
        _create(customer, "60000")

    assert raised.value.frees_at is None


# ----------------------------------------------------------------------
# Trace
# ----------------------------------------------------------------------
def test_every_refusal_is_audited():
    """La trace doit survivre a l'annulation de la transaction de base."""
    from apps.api import services as api_services

    customer = Customer.objects.create(phone=PHONE)
    _collected(customer, "50000")
    quote = services.quote_transfer(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH, net_amount=Decimal("1000")
    )

    with pytest.raises(services.LimitExceeded):
        api_services.create_transfer(
            customer=customer,
            idempotency_key="limite-audit-0001",
            source_wallet="moncash",
            destination_wallet="natcash",
            recipient_phone="50932123456",
            net_amount=Decimal("1000"),
            expected_total=quote.total_charged,
        )

    log = AuditLog.objects.get(action="limit.refused")
    assert (log.user, log.target, log.allowed) == (None, PHONE, False)
    assert log.detail["window"] == limits.DAY
    assert log.detail["requested"] == "1000.00"
    assert log.detail["cap"] == "50000.00"
    assert log.detail["frees_at"] is not None


# ----------------------------------------------------------------------
# Course entre deux creations
# ----------------------------------------------------------------------
def test_the_customer_row_is_locked_before_the_allowance_is_read(customer):
    """Sans ce verrou, deux creations simultanees passent toutes les deux."""
    with CaptureQueriesContext(connection) as captured:
        _create(customer, "1000")

    sql = [q["sql"] for q in captured.captured_queries]
    locks = [i for i, q in enumerate(sql) if "accounts_customer" in q and "FOR UPDATE" in q.upper()]
    sums = [i for i, q in enumerate(sql) if "SUM" in q.upper() and "net_amount" in q]
    if connection.vendor == "sqlite":
        # SQLite n'emet pas FOR UPDATE : il serialise les ecritures par son
        # verrou global. La garantie n'est donc pas testable ici.
        pytest.skip("select_for_update sans effet sous SQLite")
    assert locks and sums and min(locks) < min(sums)


# ----------------------------------------------------------------------
# Surface API
# ----------------------------------------------------------------------
def _api_client():
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        accounts.request_code(PHONE)
    customer, raw, _ = accounts.verify_code(PHONE, CODE)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return api, customer


def test_the_api_reports_a_limit_without_leaking_internal_state():
    api, customer = _api_client()
    _collected(customer, "50000")
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
        HTTP_IDEMPOTENCY_KEY="limite-jour-0001",
    )
    body = response.json()

    assert response.status_code == 422
    assert body["error"]["code"] == "LIMIT_EXCEEDED"
    assert body["error"]["limit"]["window"] == limits.DAY
    assert body["error"]["limit"]["remaining"] == "0.00"
    assert body["error"]["limit"]["frees_at"]
    assert "payout" not in str(body).lower() and "rank" not in str(body).lower()


def test_a_quote_is_never_refused_by_a_limit():
    api, customer = _api_client()
    _collected(customer, "50000")

    response = api.post(
        reverse("api:quotes"),
        {"source_wallet": "moncash", "destination_wallet": "natcash", "net_amount": "1000"},
        format="json",
    )

    assert response.status_code == 200


# ----------------------------------------------------------------------
# Surface site
# ----------------------------------------------------------------------
def _web_login(client):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:login"), {"phone": PHONE})
    client.post(reverse("web:login_code"), {"code": CODE})
    return Customer.objects.get(phone=PHONE)


def test_the_web_keeps_the_customer_on_the_confirmation_page(client):
    customer = _web_login(client)
    _collected(customer, "50000")
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
        reverse("web:confirm"),
        {"idempotency_key": "cle-1", "expected_total": str(quote.total_charged)},
    )
    html = response.content.decode()

    assert response.status_code == 409  # on reste sur la confirmation
    assert "plafond" in html.lower()
    assert "Payer" in html


def test_the_message_says_when_without_giving_the_exact_minute(client):
    customer = _web_login(client)
    oldest = _collected(customer, "50000", ago_seconds=3600)
    Transaction.objects.filter(pk=oldest.pk).update(
        payment_confirmed_at=timezone.localtime().replace(hour=9, minute=37, second=12) - timedelta(hours=1)
    )

    with pytest.raises(services.LimitExceeded) as raised:
        _create(customer, "1000")
    from apps.web.views import _limit_message

    message = _limit_message(raised.value)

    assert ":00." in message  # heure ronde
    assert not re.search(r"\b\d{1,2}\s*h\s*\d{2}\b", message)  # jamais la minute
    assert "37" not in message


# ----------------------------------------------------------------------
# Console
# ----------------------------------------------------------------------
def _login(client, role=Role.SUPERADMIN):
    user = User.objects.create_user(username=f"u-{role}", password="x", role=role)
    client.force_login(user)
    return user


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.APPROVER, Role.SUPPORT, Role.AUDITOR])
def test_only_the_superadmin_can_change_the_caps(client, role):
    _login(client, role)

    response = client.post(
        reverse("console:limits_update"), {"daily_cap": "90000", "monthly_cap": "900000"}, HTTP_HX_REQUEST="true"
    )

    assert response.status_code == 403
    assert TransferLimitPolicy.objects.get(pk=1).daily_cap == CAP_DAY
    assert AuditLog.objects.get(action="limits.update").allowed is False
    assert 'data-console-action="limits"' not in client.get(reverse("console:methods")).content.decode()


def test_the_superadmin_changes_the_caps_and_it_is_audited(client):
    user = _login(client)

    response = client.post(
        reverse("console:limits_update"), {"daily_cap": "90000", "monthly_cap": "900000"}, HTTP_HX_REQUEST="true"
    )

    policy = TransferLimitPolicy.objects.get(pk=1)
    assert (policy.daily_cap, policy.monthly_cap, policy.updated_by) == (
        Decimal("90000"), Decimal("900000"), user,
    )
    log = AuditLog.objects.get(action="limits.update")
    assert log.allowed is True and log.detail["daily_cap"] == "90000"
    assert response.status_code == 200


@pytest.mark.parametrize(
    "caps, expected",
    [
        ({"daily_cap": "200000", "monthly_cap": "100000"}, "mensuel"),
        ({"daily_cap": "1000", "monthly_cap": "200000"}, "maximum par transfert"),
    ],
)
def test_impossible_caps_are_refused_and_audited(client, caps, expected):
    _login(client)

    response = client.post(reverse("console:limits_update"), caps, HTTP_HX_REQUEST="true")

    assert expected in response.content.decode()
    assert "NON enregistres" in response.content.decode()
    assert TransferLimitPolicy.objects.get(pk=1).daily_cap == CAP_DAY
    assert AuditLog.objects.filter(action="limits.update", detail__outcome="error").count() == 1


def test_lowering_the_caps_never_blocks_a_transfer_in_flight(client, customer):
    txn = _collected(customer, "50000")
    _login(client)

    client.post(reverse("console:limits_update"), {"daily_cap": "50000", "monthly_cap": "60000"}, HTTP_HX_REQUEST="true")

    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.return_value = mock.Mock(
            succeeded=True, fee=Decimal("2000"), transaction_id="PP-1", api_reference="", balance_after=None
        )
        services.execute_payout(Transaction.objects.get(pk=txn.pk))

    assert Transaction.objects.get(pk=txn.pk).state == State.COMPLETED


def test_the_console_shows_a_customers_consumption_and_links_to_their_transfers(client, customer):
    txn = _collected(customer, "20000")
    _login(client, Role.OPERATOR)

    detail = client.get(reverse("console:transaction_detail", args=[txn.reference])).content.decode()
    listing = client.get(f"{reverse('console:transactions')}?customer={PHONE}").content.decode()

    assert f"?customer={PHONE}" in detail  # le numero du client est cliquable
    assert "Plafonds du client" in detail
    assert "20" in detail and "50" in detail  # consomme face au plafond
    assert txn.reference in listing
