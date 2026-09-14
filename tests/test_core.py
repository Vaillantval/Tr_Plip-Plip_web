"""Tests des invariants critiques.

Ce ne sont pas des tests de couverture : chacun protege une regle dont
la violation coute de l'argent reel.
"""

from __future__ import annotations

import hashlib
import hmac
from decimal import Decimal
from unittest import mock

import pytest

from apps.ledger import services as ledger
from apps.ledger.models import LedgerAccount
from apps.ledger.services import UnbalancedEntry
from apps.providers.plopplop import exceptions as pp
from apps.providers.plopplop.client import WithdrawalResult
from apps.providers.plopplop.signing import format_amount, sign_withdrawal
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.pricing import AmountTooSmall, quote
from apps.transactions.states import IllegalTransition, State, can

RATES = {"in": Decimal("0.03"), "out": Decimal("0.03"), "platform": Decimal("0.03")}


# ----------------------------------------------------------------------
# Signature
# ----------------------------------------------------------------------
def test_signature_matches_documented_formula():
    secret = "s" * 64
    signature, ts, formatted = sign_withdrawal(
        amount=Decimal("500.00"),
        method="natcash",
        recipient="50912345678",
        reference="CMD-001",
        client_secret=secret,
        timestamp=1715691234,
    )
    expected = hmac.new(
        secret.encode(),
        b"500|natcash|50912345678|CMD-001|1715691234",
        hashlib.sha256,
    ).hexdigest()
    assert signature == expected
    assert formatted == "500"


def test_amount_formatting_is_stable():
    # Le montant signe et le montant envoye doivent etre identiques,
    # sinon plopplop repond INVALID_SIGNATURE.
    assert format_amount(Decimal("500.00")) == "500"
    assert format_amount(500) == "500"
    assert format_amount("500.50") == "500.50"


# ----------------------------------------------------------------------
# Tarification
# ----------------------------------------------------------------------
def test_beneficiary_receives_the_announced_amount():
    q = quote(
        net_amount=Decimal("1000"),
        source_wallet="moncash",
        destination_wallet="natcash",
        rates=RATES,
    )
    # Exemple de la note conceptuelle : 1000 recus, 1090 debites.
    assert q.net_amount == Decimal("1000.00")
    assert q.total_fees == Decimal("90.00")
    assert q.total_charged == Decimal("1090.00")


def test_amount_below_provider_minimum_is_rejected():
    with pytest.raises(AmountTooSmall):
        quote(
            net_amount=Decimal("10"),
            source_wallet="moncash",
            destination_wallet="natcash",
            rates=RATES,
        )


# ----------------------------------------------------------------------
# Machine a etats
# ----------------------------------------------------------------------
def test_unknown_payout_never_retries_directly():
    # L'unique protection contre le double paiement : depuis un etat
    # indetermine, on ne peut pas retourner en file.
    assert not can(State.PAYOUT_UNKNOWN, State.PAYOUT_QUEUED)
    assert not can(State.PAYOUT_UNKNOWN, State.PAYOUT_IN_FLIGHT)
    assert can(State.PAYOUT_UNKNOWN, State.COMPLETED)
    assert can(State.PAYOUT_UNKNOWN, State.PAYOUT_FAILED)


def test_completed_is_terminal():
    assert not can(State.COMPLETED, State.PAYOUT_QUEUED)
    assert not can(State.COMPLETED, State.REFUNDED)


@pytest.mark.django_db
def test_illegal_transition_writes_nothing():
    txn = _make_txn()
    with pytest.raises(IllegalTransition):
        txn.transition(State.COMPLETED)
    txn.refresh_from_db()
    assert txn.state == State.CREATED
    assert txn.events.count() == 0


# ----------------------------------------------------------------------
# Grand livre
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_unbalanced_entry_is_refused():
    ledger.ensure_accounts()
    with pytest.raises(UnbalancedEntry):
        ledger.post(
            reference="X",
            description="desequilibre",
            lines=[(ledger.FLOAT, Decimal("100"), ""), (ledger.CLIENTS_PAYABLE, Decimal("-90"), "")],
        )


@pytest.mark.django_db
def test_journal_entry_cannot_be_edited():
    ledger.ensure_accounts()
    entry = ledger.record_float_topup(Decimal("1000"), reference="TOPUP-1")
    entry.description = "modifie"
    with pytest.raises(RuntimeError):
        entry.save()
    with pytest.raises(RuntimeError):
        entry.delete()


@pytest.mark.django_db
def test_full_conversion_keeps_books_balanced():
    ledger.ensure_accounts()
    ledger.record_float_topup(Decimal("10000"), reference="TOPUP-1")

    txn = _make_txn()
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_QUEUED

    # Apres encaissement, nous devons 1000 HTG au beneficiaire.
    payable = LedgerAccount.objects.get(code=ledger.CLIENTS_PAYABLE)
    assert payable.balance() == Decimal("-1000.00")

    fake = WithdrawalResult(
        transaction_id="API_WD_NA_1",
        api_reference="9876543210",
        reference=txn.build_payout_reference(),
        amount=Decimal("1000"),
        fee=Decimal("25"),
        total=Decimal("1025"),
        balance_after=Decimal("8975"),
        status="success",
    )
    with mock.patch("apps.transactions.services.get_client") as get_client:
        get_client.return_value.withdraw.return_value = fake
        services.execute_payout(txn)

    txn.refresh_from_db()
    assert txn.state == State.COMPLETED
    assert txn.payout_fee_actual == Decimal("25")

    # La dette est eteinte. Le float a recu les 1090 encaisses -- doc plopplop :
    # « les paiements clients creditent votre solde marchand (prepaye) » --
    # puis perdu les 1025 decaisses : 10000 + 1090 - 1025.
    assert LedgerAccount.objects.get(code=ledger.CLIENTS_PAYABLE).balance() == Decimal("0.00")
    assert LedgerAccount.objects.get(code=ledger.FLOAT).balance() == Decimal("10065.00")

    # Marge = 90 encaisses - 25 de frais reels.
    assert txn.margin_estimate == Decimal("65.00")


@pytest.mark.django_db
def test_indeterminate_payout_lands_in_unknown_not_failed():
    ledger.ensure_accounts()
    txn = _make_txn()
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn)
    txn.refresh_from_db()

    with mock.patch("apps.transactions.services.get_client") as get_client:
        get_client.return_value.withdraw.side_effect = pp.PlopPlopIndeterminate("timeout")
        services.execute_payout(txn)

    txn.refresh_from_db()
    # Surtout pas PAYOUT_QUEUED : rejouer ici, c'est payer deux fois.
    assert txn.state == State.PAYOUT_UNKNOWN


@pytest.mark.django_db
def test_duplicate_reference_is_treated_as_unknown():
    ledger.ensure_accounts()
    txn = _make_txn()
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn)
    txn.refresh_from_db()

    with mock.patch("apps.transactions.services.get_client") as get_client:
        get_client.return_value.withdraw.side_effect = pp.DuplicateReference(
            "deja utilisee", code="DUPLICATE_REFERENCE", status=409
        )
        services.execute_payout(txn)

    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_UNKNOWN


@pytest.mark.django_db
def test_each_payout_attempt_uses_a_fresh_reference():
    txn = _make_txn()
    assert txn.build_payout_reference().endswith("-W1")
    txn.payout_attempts = 1
    assert txn.build_payout_reference().endswith("-W2")


@pytest.mark.django_db
def test_payout_to_non_capable_wallet_is_refused():
    with pytest.raises(services.UnsupportedRoute):
        services.create_transaction(
            source_wallet=Wallet.MONCASH,
            destination_wallet=Wallet.CARTE,
            recipient_phone="50912345678",
            net_amount=Decimal("1000"),
        )


def _make_txn() -> Transaction:
    return services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone="50912345678",
        net_amount=Decimal("1000"),
    )
