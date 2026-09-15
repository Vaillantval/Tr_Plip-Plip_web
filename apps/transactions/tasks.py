"""Taches asynchrones.

Le point sensible est `drain_payout_queue`. Le cooldown plopplop est de
120 s PAR IP : il s'applique donc a toute la plateforme, pas a un
utilisateur. Toute concurrence sur le decaissement produit des 429 et,
pire, des tentatives dont l'issue devient indeterminee.

Dispositif :
  - une queue Celery dediee 'payouts' ;
  - un worker unique, --concurrency=1, sur cette queue ;
  - un verrou Redis global en plus, pour survivre a deux processus
    simultanes -- double demarrage par erreur, ou recouvrement de
    l'ancienne et de la nouvelle instance pendant un deploiement ;
  - un horodatage du dernier retrait, pour espacer les appels.

Le verrou a une duree de vie courte et est rafraichi a chaque etape du
lot (voir locks.py). Si le rafraichissement echoue, le lot s'arrete
AVANT le retrait suivant : le verrou appartient peut-etre deja a un
autre processus.

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

from .locks import default_backend
from .models import Transaction
from .states import State

logger = logging.getLogger(__name__)

LAST_PAYOUT_KEY = "plipplip:payout:last_at"

#: Pas maximal d'une attente de cooldown : le verrou est rafraichi entre
#: deux pas. Doit rester tres inferieur a PAYOUT_LOCK_TTL_SECONDS.
LOCK_REFRESH_STEP_SECONDS = 10

# Indirections remplacables par les tests (horloge simulee).
_now = time.time
_sleep = time.sleep


class PayoutLock:
    """Verrou global exclusif, a rafraichir pendant toute la duree du lot."""

    def __init__(self, ttl: float | None = None, backend=None):
        self.ttl = ttl if ttl is not None else settings.PAYOUT_LOCK_TTL_SECONDS
        self._backend = backend if backend is not None else default_backend()
        self.acquired = False

    def __enter__(self) -> "PayoutLock":
        try:
            self.acquired = self._backend.acquire(self.ttl)
        except Exception:
            logger.exception("Verrou des decaissements inaccessible")
            self.acquired = False
        return self

    def refresh(self) -> bool:
        """Prolonge le verrou. False s'il n'est plus a nous : arreter le lot."""
        if not self.acquired:
            return False
        try:
            still_ours = self._backend.refresh(self.ttl)
        except Exception:
            logger.exception("Rafraichissement du verrou des decaissements impossible")
            still_ours = False
        if not still_ours:
            self.acquired = False
        return still_ours

    def __exit__(self, *exc) -> None:
        if self.acquired:
            try:
                self._backend.release()
            except Exception:
                logger.exception("Liberation du verrou des decaissements impossible")
            self.acquired = False

    @staticmethod
    def wait_for_cooldown() -> float:
        """Secondes restantes avant le prochain retrait autorise."""
        last = cache.get(LAST_PAYOUT_KEY)
        if last is None:
            return 0.0
        remaining = settings.PAYOUT_COOLDOWN_SECONDS - (_now() - float(last))
        return max(0.0, remaining)


def _new_lock() -> PayoutLock:
    return PayoutLock()


def _wait_cooldown_holding(lock: PayoutLock) -> bool:
    """Attend la fin du cooldown en rafraichissant le verrou. False si perdu."""
    while True:
        if not lock.refresh():
            return False
        remaining = lock.wait_for_cooldown()
        if remaining <= 0:
            return True
        _sleep(min(remaining, LOCK_REFRESH_STEP_SECONDS))


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


@shared_task(name="transactions.drain_payout_queue", expires=settings.PAYOUT_DRAIN_INTERVAL_SECONDS)
def drain_payout_queue(max_batch: int = 5) -> dict:
    """Vide la file de decaissement, un retrait a la fois.

    Si le verrou n'est pas obtenu, on sort immediatement : un autre
    processus travaille deja, et insister ferait exactement le degat que
    ce verrou existe pour eviter.

    `expires` : beat relance la tache a chaque intervalle alors qu'un lot
    peut durer une dizaine de minutes. Une tache de drain restee en file
    au-dela d'un intervalle est perimee ; la suivante prendra le relais.
    """
    from . import services

    with _new_lock() as lock:
        if not lock.acquired:
            logger.info("File de decaissement deja traitee par un autre processus")
            return {"skipped": True}

        processed = 0
        for _ in range(max_batch):
            if not _wait_cooldown_holding(lock):
                logger.error("Verrou des decaissements perdu pendant le cooldown : lot interrompu")
                return {"processed": processed, "lock_lost": True}

            txn = Transaction.objects.payable().first()
            if txn is None:
                break

            # Dernier controle juste avant l'appel a plopplop.
            if not lock.refresh():
                logger.error("Verrou des decaissements perdu avant %s : lot interrompu", txn.reference)
                return {"processed": processed, "lock_lost": True}

            try:
                services.execute_payout(txn)
            except Exception:
                logger.exception("Echec du decaissement sur %s", txn.reference)
            finally:
                cache.set(LAST_PAYOUT_KEY, _now(), None)
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
