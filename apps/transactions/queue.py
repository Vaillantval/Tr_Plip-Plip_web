"""File de decaissement : rang, delai estime, etat du worker.

SEUL module qui enumere la file et attribue un rang. L'ecran File de la
console, l'API et le site client en consomment les resultats. Le rang et
la profondeur ne sortent jamais vers les clients : voir
apps.api.status.public_wait.

Estimation client : une PROMESSE, pas un compte a rebours.
  - Elle est fixee une fois, a la premiere estimation affichable --
    normalement l'entree en file -- dans Transaction.payout_eta_deadline.
  - L'affichage en decoule et ne peut que baisser avec le temps.
  - Si la realite depasse la promesse (decaissement echoue remis devant,
    worker a l'arret...), l'estimation est retiree definitivement
    (payout_eta_withdrawn) : message sans duree, jamais une fourchette
    plus large.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from .models import Transaction, TransactionEvent
from .states import State

ZERO = Decimal("0")
#: Marge proportionnelle appliquee au delai brut avant arrondi.
MARGIN_RATIO = 1.2
#: Etats pour lesquels une duree peut etre annoncee.
ESTIMABLE_STATES = (State.PAYOUT_QUEUED, State.PAYOUT_IN_FLIGHT)


@dataclass(frozen=True)
class QueueEntry:
    txn: Transaction
    rank: int
    eta_seconds: int
    covered: bool


@dataclass(frozen=True)
class WorkerStatus:
    stalled: bool
    depth: int
    idle_seconds: int | None
    last_attempt_at: datetime | None


@dataclass
class QueueSnapshot:
    taken_at: datetime
    entries: list[QueueEntry]
    coverage: dict
    worker: WorkerStatus
    _by_id: dict = field(default_factory=dict, repr=False)

    def entry_for(self, txn_id: int) -> QueueEntry | None:
        if not self._by_id:
            self._by_id = {e.txn.pk: e for e in self.entries}
        return self._by_id.get(txn_id)


# ----------------------------------------------------------------------
# Photo de la file
# ----------------------------------------------------------------------
def queue_snapshot(now: datetime | None = None) -> QueueSnapshot:
    """Une seule lecture de la file : rang, delai brut et couverture.

    Delai brut = rang x PAYOUT_COOLDOWN_SECONDS (un retrait par cooldown).
    Couverture : le float disponible couvre le cumul net + cout de retrait
    jusqu'a ce rang inclus (voir treasury.queue_coverage).
    """
    from apps.treasury import services as treasury

    now = now or timezone.now()
    queued = list(Transaction.objects.payable().with_state_since())
    funds = treasury.float_available_for_queue()
    available = funds["available"]
    cooldown = settings.PAYOUT_COOLDOWN_SECONDS

    entries = []
    total_net = total_debit = ZERO
    stall_rank = None
    for rank, txn in enumerate(queued, start=1):
        total_net += txn.net_amount
        total_debit += txn.net_amount + txn.provider_fee_out_estimate
        if stall_rank is None and total_debit > available:
            stall_rank = rank
        entries.append(QueueEntry(txn=txn, rank=rank, eta_seconds=rank * cooldown, covered=stall_rank is None))

    depth = len(entries)
    coverage = {
        **funds,
        "depth": depth,
        "total_net": total_net,
        "total_debit": total_debit,
        "stall_rank": stall_rank,
        "covered_count": depth if stall_rank is None else stall_rank - 1,
        "shortfall": max(total_debit - available, ZERO),
    }
    return QueueSnapshot(taken_at=now, entries=entries, coverage=coverage, worker=_worker_status(queued, now))


# ----------------------------------------------------------------------
# Worker de decaissement
# ----------------------------------------------------------------------
def payout_worker_status(now: datetime | None = None) -> WorkerStatus:
    """Lecture legere pour l'alerte de la console."""
    now = now or timezone.now()
    queued = list(Transaction.objects.payable().with_state_since().only("id", "updated_at"))
    return _worker_status(queued, now)


def _worker_status(queued: list[Transaction], now: datetime) -> WorkerStatus:
    """Worker a l'arret : file non vide et aucun retrait tente depuis
    PAYOUT_STALL_SECONDS, compte a partir du plus tardif entre la derniere
    tentative et l'entree en file la plus ancienne.

    Lu dans la base (evenements PAYOUT_IN_FLIGHT), pas dans le cache : un
    Redis vide ou redemarre ne masque pas la panne.
    """
    if not queued:
        return WorkerStatus(stalled=False, depth=0, idle_seconds=None, last_attempt_at=None)
    last_attempt = (
        TransactionEvent.objects.filter(to_state=State.PAYOUT_IN_FLIGHT)
        .order_by("-created_at", "-id")
        .values_list("created_at", flat=True)
        .first()
    )
    waiting_since = min(t.state_since or t.updated_at for t in queued)
    reference = max(filter(None, (last_attempt, waiting_since)))
    idle = max(0, int((now - reference).total_seconds()))
    return WorkerStatus(
        stalled=idle >= settings.PAYOUT_STALL_SECONDS,
        depth=len(queued),
        idle_seconds=idle,
        last_attempt_at=last_attempt,
    )


# ----------------------------------------------------------------------
# Estimation
# ----------------------------------------------------------------------
def padded_seconds(seconds: int) -> int:
    """Delai brut + marge : 20 % et un intervalle de beat."""
    return math.ceil(seconds * MARGIN_RATIO + settings.PAYOUT_DRAIN_INTERVAL_SECONDS)


def bucket_upper_minutes(seconds: float) -> int:
    """Borne haute de la tranche, arrondie VERS LE HAUT.

    Moins de 5 min, puis tranches de 5 min jusqu'a 30, puis de 15 min.
    """
    minutes = max(1, math.ceil(seconds / 60))
    if minutes <= 5:
        return 5
    if minutes <= 30:
        return math.ceil(minutes / 5) * 5
    return 30 + math.ceil((minutes - 30) / 15) * 15


def bucket_range(upper_minutes: int) -> dict:
    if upper_minutes <= 5:
        return {"min_minutes": 0, "max_minutes": 5}
    width = 5 if upper_minutes <= 30 else 15
    return {"min_minutes": upper_minutes - width, "max_minutes": upper_minutes}


def estimated_wait(txn: Transaction, snapshot: QueueSnapshot | None = None) -> dict:
    """Delai avant decaissement.

    Retour :
      seconds  delai brut (rang x cooldown), 0 si le retrait est en cours,
               None hors file. Usage interne : il revele le rang.
      covered  le float couvre cette transaction.
      display  {"min_minutes", "max_minutes"} affichable, ou None.

    Peut ecrire, une fois chacun : la promesse initiale, puis son retrait.
    Jamais `state`.
    """
    unavailable = {"seconds": None, "covered": False, "display": None}
    if txn.state not in ESTIMABLE_STATES:
        return unavailable

    snapshot = snapshot or queue_snapshot()
    now = snapshot.taken_at
    if txn.state == State.PAYOUT_IN_FLIGHT:
        seconds, covered = 0, True
    else:
        entry = snapshot.entry_for(txn.pk)
        if entry is None:  # sortie de la file depuis la photo
            return unavailable
        seconds, covered = entry.eta_seconds, entry.covered

    result = {"seconds": seconds, "covered": covered, "display": None}
    if txn.payout_eta_withdrawn:
        return result

    deadline = txn.payout_eta_deadline
    reachable = now + timedelta(seconds=seconds + settings.PAYOUT_DRAIN_INTERVAL_SECONDS)
    if deadline is not None and reachable > deadline:
        # La realite depasse la promesse : on retire, on n'elargit pas.
        _withdraw(txn)
        return result

    if not covered or snapshot.worker.stalled:
        return result

    if deadline is None:
        upper = bucket_upper_minutes(padded_seconds(seconds))
        if upper * 60 > settings.ETA_MAX_DISPLAY_SECONDS:
            return result
        deadline = _promise(txn, now + timedelta(minutes=upper))
        if deadline is None:
            return result

    remaining = (deadline - now).total_seconds()
    result["display"] = bucket_range(bucket_upper_minutes(remaining))
    return result


def _promise(txn: Transaction, deadline: datetime) -> datetime | None:
    """Fixe la promesse si aucune ne l'est deja. Retourne celle qui fait foi."""
    updated = Transaction.objects.filter(
        pk=txn.pk, payout_eta_deadline__isnull=True, payout_eta_withdrawn=False
    ).update(payout_eta_deadline=deadline)
    if not updated:
        txn.refresh_from_db(fields=["payout_eta_deadline", "payout_eta_withdrawn"])
        return None if txn.payout_eta_withdrawn else txn.payout_eta_deadline
    txn.payout_eta_deadline = deadline
    return deadline


def _withdraw(txn: Transaction) -> None:
    Transaction.objects.filter(pk=txn.pk, payout_eta_withdrawn=False).update(payout_eta_withdrawn=True)
    txn.payout_eta_withdrawn = True


def announce_on_queue_entry(txn: Transaction) -> None:
    """Fixe la promesse a l'entree en file. N'empeche jamais la mise en file."""
    import logging

    try:
        with db_transaction.atomic():
            estimated_wait(txn)
    except Exception:
        logging.getLogger(__name__).exception("Estimation du delai impossible pour %s", txn.reference)
