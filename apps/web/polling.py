"""Cadence de rafraichissement de la page de suivi.

Reactive tant que le client doit payer ; degressive ensuite, d'apres le
temps ecoule depuis la confirmation du paiement (et non depuis le
chargement de la page : un rechargement ne remet pas la cadence a zero).
"""

from __future__ import annotations

from django.utils import timezone

from apps.api.status import PublicStatus

FAST_SECONDS = 5
MEDIUM_SECONDS = 15
SLOW_SECONDS = 30
FAST_UNTIL_SECONDS = 60
MEDIUM_UNTIL_SECONDS = 300


def poll_interval(txn, status: str, now=None) -> int | None:
    """Secondes entre deux rafraichissements ; None : plus de rafraichissement."""
    if status == PublicStatus.AWAITING_PAYMENT:
        return FAST_SECONDS
    if status != PublicStatus.IN_PROGRESS:
        return None
    now = now or timezone.now()
    confirmed_at = txn.payment_confirmed_at or now
    elapsed = (now - confirmed_at).total_seconds()
    if elapsed < FAST_UNTIL_SECONDS:
        return FAST_SECONDS
    if elapsed < MEDIUM_UNTIL_SECONDS:
        return MEDIUM_SECONDS
    return SLOW_SECONDS
