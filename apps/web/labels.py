"""Libelles traduits du site client."""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

from apps.api.status import PublicStatus

WALLET_LABELS = {
    "moncash": "MonCash",
    "natcash": "NatCash",
    "kashpaw": "Kashpaw",
    "carte": _("Carte bancaire"),
}

STATUS_LABELS = {
    PublicStatus.AWAITING_PAYMENT: _("En attente de paiement"),
    PublicStatus.IN_PROGRESS: _("En cours"),
    PublicStatus.DELIVERED: _("Livré"),
    PublicStatus.EXPIRED: _("Expiré"),
    PublicStatus.REFUNDED: _("Remboursé"),
    PublicStatus.CANCELLED: _("Annulé"),
}

def wallet_label(code: str) -> str:
    return WALLET_LABELS.get(code, code)
