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


def public_wait(txn, cache: dict | None = None) -> dict | None:
    """Delai estime cote client, pour l'API comme pour le site web.

    Jamais de rang, de profondeur ni de delai brut : une fourchette
    arrondie vers le haut, ou `available: False` sans explication.
    None hors statut « en cours ».

    `cache` : dictionnaire partage par une requete (contexte du
    serializer) pour ne lire la file qu'une fois, meme sur une liste.
    """
    from apps.transactions import queue

    if public_status(txn.state) != PublicStatus.IN_PROGRESS:
        return None
    snapshot = None
    if txn.state in queue.ESTIMABLE_STATES and cache is not None:
        snapshot = cache.get("queue_snapshot") or cache.setdefault("queue_snapshot", queue.queue_snapshot())
    display = queue.estimated_wait(txn, snapshot)["display"]
    if display is None:
        return {"available": False, "min_minutes": None, "max_minutes": None}
    return {"available": True, **display}


def payment_instructions(txn, status: str | None = None) -> dict:
    """Comment payer, pour l'API comme pour le site web.

    mode : redirect (ouvrir redirect_url), ussd (valider sur le telephone),
    unavailable (lien non obtenu : ne pas recreer de transfert tout de
    suite), none (plus de paiement attendu).
    """
    status = status or public_status(txn.state)
    if status != PublicStatus.AWAITING_PAYMENT:
        return {"mode": "none", "redirect_url": None, "expires_at": None}
    redirect_url = txn.payment_redirect_url or ""
    if not redirect_url.startswith(("https://", "http://")):
        redirect_url = ""
    if txn.state == State.CREATED or not txn.payment_provider_id:
        mode = "unavailable"
    elif redirect_url:
        mode = "redirect"
    else:
        mode = "ussd"
    return {
        "mode": mode,
        "redirect_url": redirect_url or None,
        "expires_at": txn.payment_expires_at,
    }
