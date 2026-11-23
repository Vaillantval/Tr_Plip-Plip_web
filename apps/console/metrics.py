"""Chiffres de la console : lectures seules sur les modeles existants.

Rien ici n'ecrit, ne verrouille ni n'appelle un fournisseur : ces
fonctions tournent a chaque affichage de page.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.transactions import services as txn_services
from apps.transactions.models import Transaction
from apps.transactions.states import State


def exceptions_to_handle(now=None) -> int:
    """Transferts qui attendent un humain : la pastille du menu.

    Plus etroit que l'ecran Exceptions, qui montre aussi les retraits en
    cours depuis peu : ceux-la sont normaux, une pastille ne doit pas
    s'allumer a chaque decaissement.
    """
    now = now or timezone.now()
    pending_before = now - timedelta(seconds=settings.PAYOUT_PENDING_STALE_SECONDS)
    in_flight_before = now - timedelta(seconds=settings.PAYOUT_INFLIGHT_STALE_SECONDS)
    return (
        Transaction.objects.with_state_since()
        .annotate(since=Coalesce("state_since", "updated_at"))
        .filter(
            Q(state__in=(State.PAYOUT_FAILED, State.PAYOUT_UNKNOWN))
            | Q(state=State.PAYMENT_CONFIRMED, failure_code__in=txn_services.HOLD_CODES)
            | Q(state=State.PAYOUT_PENDING, since__lt=pending_before)
            | Q(state=State.PAYOUT_IN_FLIGHT, since__lt=in_flight_before)
        )
        .count()
    )


def open_claims() -> int:
    from apps.claims.models import Claim, Status

    return Claim.objects.filter(status=Status.OPEN).count()
