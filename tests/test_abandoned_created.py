"""Transferts restes a l'etat Cree : annules apres le delai de paiement.

Cas reel : le lien NatCash trop long faisait echouer la sauvegarde apres
la creation du paiement chez plopplop. La transaction restait Cree, sans
lien, et bloquait son montant dans les plafonds pour toujours.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.conf import settings
from django.utils import timezone

from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State
from apps.transactions.tasks import poll_pending_payments


def _created(age_seconds: int) -> Transaction:
    txn = services.create_transaction(
        source_wallet=Wallet.NATCASH,
        destination_wallet=Wallet.MONCASH,
        recipient_phone="50937123456",
        net_amount=Decimal("100"),
    )
    Transaction.objects.filter(pk=txn.pk).update(created_at=timezone.now() - timedelta(seconds=age_seconds))
    return txn


@pytest.mark.django_db
def test_created_past_payment_delay_is_cancelled():
    txn = _created(settings.PAYMENT_EXPIRY_SECONDS + 60)

    assert services.cancel_abandoned_created() == 1

    txn.refresh_from_db()
    assert (txn.state, txn.failure_code) == (State.CANCELLED, "PAYMENT_NOT_STARTED")


@pytest.mark.django_db
def test_recent_created_is_left_alone():
    txn = _created(60)

    assert services.cancel_abandoned_created() == 0

    txn.refresh_from_db()
    assert txn.state == State.CREATED


@pytest.mark.django_db
def test_awaiting_payment_is_never_touched_by_the_cleanup():
    txn = _created(settings.PAYMENT_EXPIRY_SECONDS + 60)
    txn.transition(State.AWAITING_PAYMENT)

    assert services.cancel_abandoned_created() == 0


@pytest.mark.django_db
def test_the_payment_poll_runs_the_cleanup():
    _created(settings.PAYMENT_EXPIRY_SECONDS + 60)

    assert poll_pending_payments()["abandoned"] == 1
