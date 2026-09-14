"""Statuts exposes aux clients.

Les etats internes ne sortent jamais de l'API. En particulier
PAYOUT_UNKNOWN et PAYOUT_FAILED s'affichent « en cours » : le client a
paye, nous detenons son argent et un operateur traite le cas. Lui
afficher « echoue » provoquerait des reclamations pour une situation qui
se resout de notre cote.
"""

from __future__ import annotations

from apps.transactions.states import State


class PublicStatus:
    AWAITING_PAYMENT = "awaiting_payment"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    EXPIRED = "expired"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


LABELS = {
    PublicStatus.AWAITING_PAYMENT: "En attente de paiement",
    PublicStatus.IN_PROGRESS: "En cours",
    PublicStatus.DELIVERED: "Livre",
    PublicStatus.EXPIRED: "Expire",
    PublicStatus.REFUNDED: "Rembourse",
    PublicStatus.CANCELLED: "Annule",
}

MAPPING = {
    State.CREATED: PublicStatus.AWAITING_PAYMENT,
    State.AWAITING_PAYMENT: PublicStatus.AWAITING_PAYMENT,
    State.PAYMENT_CONFIRMED: PublicStatus.IN_PROGRESS,
    State.PAYOUT_QUEUED: PublicStatus.IN_PROGRESS,
    State.PAYOUT_IN_FLIGHT: PublicStatus.IN_PROGRESS,
    State.PAYOUT_UNKNOWN: PublicStatus.IN_PROGRESS,
    State.PAYOUT_PENDING: PublicStatus.IN_PROGRESS,
    State.PAYOUT_FAILED: PublicStatus.IN_PROGRESS,
    State.COMPLETED: PublicStatus.DELIVERED,
    State.PAYMENT_EXPIRED: PublicStatus.EXPIRED,
    State.REFUNDED: PublicStatus.REFUNDED,
    State.CANCELLED: PublicStatus.CANCELLED,
}


def public_status(state: str) -> str:
    return MAPPING[State(state)]
