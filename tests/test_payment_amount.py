"""Encaissement d'un montant different du devis.

Regle : tout ecart bloque la transaction avant la file de decaissement,
dans les deux sens, et un montant non communique par plopplop aussi.
Rien n'est verse au beneficiaire tant qu'un operateur n'a pas tranche.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest
from django.urls import reverse

from apps.accounts.models import AuditLog, Role, User
from apps.ledger import services as ledger
from apps.ledger.models import JournalEntry, LedgerAccount
from apps.providers.plopplop.client import PaymentStatus
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

GET_CLIENT = "apps.transactions.services.get_client"
HTMX = {"HTTP_HX_REQUEST": "true"}
EXPECTED = Decimal("1090.00")


@pytest.fixture
def books(db):
    ledger.ensure_accounts()


@pytest.fixture
def operator(db):
    return User.objects.create_user(username="operateur", password="x", role=Role.OPERATOR)


def _awaiting() -> Transaction:
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone="50932123456",
        sender_phone="50937123456",
        net_amount=Decimal("1000"),
    )
    txn.transition(State.AWAITING_PAYMENT)
    txn.refresh_from_db()
    assert txn.total_charged == EXPECTED
    return txn


def _poll(txn: Transaction, amount) -> Transaction:
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.payment_status.return_value = PaymentStatus(
            reference=txn.reference, transaction_id="PAY-1", confirmed=True, amount=amount, method="moncash"
        )
        services.poll_payment(txn)
    txn.refresh_from_db()
    return txn


def _balance(code) -> Decimal:
    return LedgerAccount.objects.get(code=code).balance()


def _assert_books_balanced():
    assert all(entry.is_balanced() for entry in JournalEntry.objects.all())


# ----------------------------------------------------------------------
# Blocage
# ----------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("received", [Decimal("100.00"), Decimal("1089.99"), Decimal("1200.00")])
def test_any_amount_difference_holds_the_transfer_before_the_queue(books, received):
    txn = _poll(_awaiting(), received)

    assert txn.state == State.PAYMENT_CONFIRMED
    assert (txn.failure_code, txn.payment_amount_received) == (services.AMOUNT_MISMATCH, received)
    assert services.is_payment_held(txn)
    assert not Transaction.objects.payable().exists()
    # Le grand livre constate ce qui est entre, integralement du au payeur,
    # sans commission.
    assert _balance(ledger.CASH_SETTLEMENT) == received
    assert _balance(ledger.CLIENTS_PAYABLE) == -received
    assert _balance(ledger.REVENUE_COMMISSION) == Decimal("0")
    _assert_books_balanced()


@pytest.mark.django_db
def test_amount_not_reported_by_plopplop_holds_the_transfer(books):
    txn = _poll(_awaiting(), None)

    assert (txn.state, txn.failure_code, txn.payment_amount_received) == (
        State.PAYMENT_CONFIRMED,
        services.AMOUNT_UNVERIFIED,
        None,
    )
    assert not Transaction.objects.payable().exists()
    assert not JournalEntry.objects.exists()  # rien de connu, rien d'ecrit


@pytest.mark.django_db
def test_exact_amount_still_goes_straight_to_the_queue(books):
    txn = _poll(_awaiting(), EXPECTED)

    assert (txn.state, txn.failure_code, txn.payment_amount_received) == (State.PAYOUT_QUEUED, "", EXPECTED)


@pytest.mark.django_db
def test_held_transfer_is_shown_in_progress_to_the_customer_and_first_on_exceptions(client, books, operator):
    held = _poll(_awaiting(), Decimal("100.00"))
    client.force_login(operator)

    response = client.get(reverse("console:exceptions"))

    assert [row["txn"].pk for row in response.context["held"]] == [held.pk]
    html = response.content.decode()
    assert html.index('data-group="held"') < html.index(held.reference)
    from apps.api.status import public_status

    assert public_status(held.state) == "in_progress"


# ----------------------------------------------------------------------
# Remboursement d'un encaissement bloque
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_refund_of_a_mismatch_returns_what_was_received_and_zeroes_the_books(client, books, operator):
    txn = _poll(_awaiting(), Decimal("100.00"))
    client.force_login(operator)

    client.post(
        reverse("console:transaction_refund", args=[txn.reference]),
        {"reason": "Paiement partiel", "transfer_reference": "MC-555"},
        **HTMX,
    )

    txn.refresh_from_db()
    assert txn.state == State.REFUNDED
    for code in (ledger.CASH_SETTLEMENT, ledger.CLIENTS_PAYABLE, ledger.REVENUE_COMMISSION):
        assert _balance(code) == Decimal("0"), code
    refund = JournalEntry.objects.get(reference=f"{txn.reference}-REFUND")
    assert "MC-555" in refund.description
    assert txn.events.last().data["refunded_amount"] == "100.00"
    _assert_books_balanced()


@pytest.mark.django_db
def test_refund_of_a_mismatch_for_another_amount_is_refused(books):
    txn = _poll(_awaiting(), Decimal("100.00"))

    with pytest.raises(services.InvalidRefund):
        services.refund(txn, reason="Partiel", transfer_reference="MC-1", refunded_amount=Decimal("1090.00"))

    txn.refresh_from_db()
    assert services.is_payment_held(txn)
    assert not JournalEntry.objects.filter(reference=f"{txn.reference}-REFUND").exists()


@pytest.mark.django_db
def test_refund_of_an_unverified_amount_requires_the_verified_amount(client, books, operator):
    txn = _poll(_awaiting(), None)
    client.force_login(operator)
    url = reverse("console:transaction_refund", args=[txn.reference])

    refused = client.post(url, {"reason": "Montant inconnu", "transfer_reference": "MC-9"}, **HTMX)
    txn.refresh_from_db()
    assert services.is_payment_held(txn)
    assert "NON enregistre" in refused.content.decode()

    client.post(url, {"reason": "Montant inconnu", "transfer_reference": "MC-9", "refunded_amount": "1090.00"}, **HTMX)

    txn.refresh_from_db()
    assert txn.state == State.REFUNDED
    assert _balance(ledger.CASH_SETTLEMENT) == Decimal("0")
    assert _balance(ledger.CLIENTS_PAYABLE) == Decimal("0")
    assert JournalEntry.objects.filter(transaction=txn).count() == 2  # constat + remboursement
    _assert_books_balanced()
    logs = list(AuditLog.objects.filter(action="transaction.refund").order_by("id"))
    assert logs[-1].detail["refunded_amount"] == "1090.00"


# ----------------------------------------------------------------------
# Deblocage apres verification
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_release_after_verifying_the_exact_amount_queues_with_normal_books(client, books, operator):
    txn = _poll(_awaiting(), Decimal("1089.00"))  # montant errone declare par plopplop
    client.force_login(operator)

    response = client.post(
        reverse("console:payment_release", args=[txn.reference]),
        {"verified_amount": "1090.00", "reason": "Confirme par le support plopplop, ticket 4411"},
        **HTMX,
    )

    assert response.status_code == 200
    txn.refresh_from_db()
    assert (txn.state, txn.failure_code, txn.payment_amount_received) == (State.PAYOUT_QUEUED, "", EXPECTED)
    assert txn.events.last().actor == operator
    # Blocage extourne, encaissement normal : dette = net, commission = frais.
    assert _balance(ledger.CASH_SETTLEMENT) == EXPECTED
    assert _balance(ledger.CLIENTS_PAYABLE) == Decimal("-1000.00")
    assert _balance(ledger.REVENUE_COMMISSION) == Decimal("-90.00")
    assert JournalEntry.objects.filter(reference=f"{txn.reference}-HOLD-REV").exists()
    _assert_books_balanced()
    assert AuditLog.objects.get(action="payment.release").allowed


@pytest.mark.django_db
@pytest.mark.parametrize("verified", ["100.00", "1200.00"])
def test_release_with_a_real_difference_is_refused(client, books, operator, verified):
    txn = _poll(_awaiting(), Decimal(verified))
    client.force_login(operator)

    response = client.post(
        reverse("console:payment_release", args=[txn.reference]),
        {"verified_amount": verified, "reason": "Verifie"},
        **HTMX,
    )

    assert "REFUSE" in response.content.decode()
    txn.refresh_from_db()
    assert services.is_payment_held(txn)
    assert not Transaction.objects.payable().exists()
    assert AuditLog.objects.filter(action="payment.release", detail__outcome="error").count() == 1


@pytest.mark.django_db
def test_release_of_an_unverified_amount_writes_the_normal_receipt(books):
    txn = _poll(_awaiting(), None)

    services.release_held_payment(txn, verified_amount=EXPECTED, reason="Releve plopplop du jour")

    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_QUEUED
    assert list(JournalEntry.objects.filter(transaction=txn).values_list("reference", flat=True)) == [txn.reference]
    _assert_books_balanced()


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.SUPPORT, Role.AUDITOR, Role.APPROVER])
def test_read_only_roles_cannot_release_or_see_the_button(client, books, role):
    txn = _poll(_awaiting(), None)
    client.force_login(User.objects.create_user(username=f"u-{role}", password="x", role=role))

    page = client.get(reverse("console:transaction_detail", args=[txn.reference]))
    response = client.post(
        reverse("console:payment_release", args=[txn.reference]),
        {"verified_amount": "1090.00", "reason": "x"},
        **HTMX,
    )

    assert b"data-console-action" not in page.content
    assert response.status_code == 403
    txn.refresh_from_db()
    assert services.is_payment_held(txn)
    assert AuditLog.objects.get(action="payment.release").allowed is False


@pytest.mark.django_db
def test_release_is_refused_for_a_transfer_that_is_not_held(books):
    txn = _poll(_awaiting(), EXPECTED)
    with pytest.raises(services.InvalidRelease):
        services.release_held_payment(txn, verified_amount=EXPECTED, reason="x")
