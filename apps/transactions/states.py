"""Machine a etats d'une conversion Plip-Plip.

Une seule fonction fait foi : transition(). Aucun autre endroit du code
ne doit affecter `Transaction.state` directement.
"""

from __future__ import annotations

from django.db import models


class State(models.TextChoices):
    CREATED = "created", "Cree"
    AWAITING_PAYMENT = "awaiting_payment", "Attente paiement"
    PAYMENT_CONFIRMED = "payment_confirmed", "Paiement confirme"
    PAYOUT_QUEUED = "payout_queued", "File de decaissement"
    PAYOUT_IN_FLIGHT = "payout_in_flight", "Decaissement en cours"
    PAYOUT_UNKNOWN = "payout_unknown", "Decaissement indetermine"
    PAYOUT_PENDING = "payout_pending", "Decaissement en attente operateur"
    COMPLETED = "completed", "Terminee"
    PAYMENT_EXPIRED = "payment_expired", "Paiement expire"
    PAYOUT_FAILED = "payout_failed", "Echec decaissement"
    REFUNDED = "refunded", "Remboursee"
    CANCELLED = "cancelled", "Annulee"


#: Etats ou l'argent du client est chez nous et n'est pas encore arrive
#: chez le beneficiaire. Ce sont les etats a surveiller en exploitation :
#: chacun represente une dette envers l'utilisateur.
LIABILITY_STATES = frozenset(
    {
        State.PAYMENT_CONFIRMED,
        State.PAYOUT_QUEUED,
        State.PAYOUT_IN_FLIGHT,
        State.PAYOUT_UNKNOWN,
        State.PAYOUT_PENDING,
        State.PAYOUT_FAILED,
    }
)

#: Etats terminaux : plus aucune transition automatique.
TERMINAL_STATES = frozenset(
    {State.COMPLETED, State.PAYMENT_EXPIRED, State.REFUNDED, State.CANCELLED}
)

#: PAYOUT_UNKNOWN ne sort JAMAIS vers un nouvel essai direct : la seule
#: issue est une verification aupres de l'operateur.
ALLOWED: dict[str, frozenset[str]] = {
    State.CREATED: frozenset({State.AWAITING_PAYMENT, State.CANCELLED}),
    State.AWAITING_PAYMENT: frozenset(
        {State.PAYMENT_CONFIRMED, State.PAYMENT_EXPIRED, State.CANCELLED}
    ),
    State.PAYMENT_CONFIRMED: frozenset({State.PAYOUT_QUEUED, State.REFUNDED}),
    State.PAYOUT_QUEUED: frozenset({State.PAYOUT_IN_FLIGHT, State.REFUNDED}),
    State.PAYOUT_IN_FLIGHT: frozenset(
        {
            State.COMPLETED,
            State.PAYOUT_PENDING,
            State.PAYOUT_FAILED,
            State.PAYOUT_UNKNOWN,
        }
    ),
    State.PAYOUT_UNKNOWN: frozenset(
        {State.COMPLETED, State.PAYOUT_PENDING, State.PAYOUT_FAILED}
    ),
    State.PAYOUT_PENDING: frozenset({State.COMPLETED, State.PAYOUT_FAILED}),
    State.PAYOUT_FAILED: frozenset({State.PAYOUT_QUEUED, State.REFUNDED}),
    State.COMPLETED: frozenset(),
    State.PAYMENT_EXPIRED: frozenset(),
    State.REFUNDED: frozenset(),
    State.CANCELLED: frozenset(),
}


class IllegalTransition(Exception):
    def __init__(self, current: str, target: str):
        super().__init__(f"Transition interdite : {current} -> {target}")
        self.current = current
        self.target = target


def can(current: str, target: str) -> bool:
    return target in ALLOWED.get(current, frozenset())


def check(current: str, target: str) -> None:
    if not can(current, target):
        raise IllegalTransition(current, target)
