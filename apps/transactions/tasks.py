"""Taches asynchrones.

Le point sensible est `drain_payout_queue`. Le cooldown plopplop est de
120 s PAR IP : il s'applique donc a toute la plateforme, pas a un
utilisateur. Toute concurrence sur le decaissement produit des 429 et,
pire, des tentatives dont l'issue devient indeterminee.

Dispositif :
  - une queue Celery dediee 'payouts' ;
  - un worker unique, --concurrency=1, sur cette queue ;
  - un verrou Redis global en plus, pour survivre a un double demarrage
    de worker (erreur de deploiement classique) ;
  - un horodatage du dernier retrait, pour espacer les appels.

Debit maximal en resultant : environ 30 decaissements par heure. C'est
la contrainte structurelle du MVP, et la raison pour laquelle le module
Payroll ne peut pas etre construit sur ce dispositif en l'etat.
"""

from __future__ import annotations

import logging
import time

from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from .models import Transaction
from .states import State

logger = logging.getLogger(__name__)

PAYOUT_LOCK_KEY = "plipplip:payout:lock"
LAST_PAYOUT_KEY = "plipplip:payout:last_at"


class PayoutLock:
    """Verrou global exclusif, avec espacement force entre deux retraits."""

    def __init__(self, timeout: int = 300):
        self.timeout = timeout
        self.acquired = False

    def __enter__(self) -> "PayoutLock":
        self.acquired = cache.add(PAYOUT_LOCK_KEY, timezone.now().isoformat(), self.timeout)
        return self

    def __exit__(self, *exc) -> None:
        if self.acquired:
            cache.set(LAST_PAYOUT_KEY, time.time(), None)
            cache.delete(PAYOUT_LOCK_KEY)

    def wait_for_cooldown(self) -> float:
        """Retourne le nombre de secondes restantes avant le prochain retrait."""
        last = cache.get(LAST_PAYOUT_KEY)
        if last is None:
            return 0.0
        elapsed = time.time() - float(last)
        remaining = settings.PAYOUT_COOLDOWN_SECONDS - elapsed
        return max(0.0, remaining)


@shared_task(name="transactions.poll_pending_payments")
def poll_pending_payments(limit: int = 200) -> dict:
    """Interroge les paiements en attente. Aucun webhook cote plopplop,
    le polling est la seule option.
    """
    from . import services

    pending = Transaction.objects.awaiting_payment().order_by("last_polled_at")[:limit]
    confirmed = expired = errors = 0

    for txn in pending:
        try:
            services.poll_payment(txn)
            if txn.state == State.PAYMENT_CONFIRMED or txn.state == State.PAYOUT_QUEUED:
                confirmed += 1
            elif txn.state == State.PAYMENT_EXPIRED:
                expired += 1
        except Exception:
            errors += 1
            logger.exception("Echec du polling sur %s", txn.reference)

    return {"polled": len(pending), "confirmed": confirmed, "expired": expired, "errors": errors}


@shared_task(name="transactions.drain_payout_queue")
def drain_payout_queue(max_batch: int = 5) -> dict:
    """Vide la file de decaissement, un retrait a la fois.

    Si le verrou n'est pas obtenu, on sort immediatement : un autre
    worker travaille deja, et insister ferait exactement le degat que
    ce verrou existe pour eviter.
    """
    from . import services

    with PayoutLock() as lock:
        if not lock.acquired:
            logger.info("File de decaissement deja traitee par un autre worker")
            return {"skipped": True}

        processed = 0
        for _ in range(max_batch):
            remaining = lock.wait_for_cooldown()
            if remaining > 0:
                time.sleep(min(remaining, settings.PAYOUT_COOLDOWN_SECONDS))

            txn = Transaction.objects.payable().first()
            if txn is None:
                break

            try:
                services.execute_payout(txn)
            except Exception:
                logger.exception("Echec du decaissement sur %s", txn.reference)
            finally:
                cache.set(LAST_PAYOUT_KEY, time.time(), None)
                processed += 1

        return {"processed": processed}


@shared_task(name="transactions.resolve_unknown_payouts")
def resolve_unknown_payouts(limit: int = 50) -> dict:
    """Tranche les decaissements d'issue inconnue.

    Tache separee de la file : elle ne fait que des lectures
    (withdraw/marchand/verify) et n'est donc pas soumise au cooldown.
    """
    from . import services

    stuck = Transaction.objects.filter(
        state__in=[State.PAYOUT_UNKNOWN, State.PAYOUT_PENDING]
    ).order_by("updated_at")[:limit]

    resolved = 0
    for txn in stuck:
        try:
            services.verify_unknown_payout(txn)
            if txn.state in (State.COMPLETED, State.PAYOUT_FAILED):
                resolved += 1
        except Exception:
            logger.exception("Verification impossible sur %s", txn.reference)

    return {"checked": len(stuck), "resolved": resolved}


@shared_task(name="transactions.check_float_level")
def check_float_level() -> dict:
    """Alerte sur le niveau de float. Voir apps.treasury."""
    from apps.treasury.services import evaluate_float

    return evaluate_float()
