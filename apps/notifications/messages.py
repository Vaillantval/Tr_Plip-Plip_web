"""Textes des SMS.

SANS ACCENTS, volontairement : un seul caractere accentue fait basculer le
SMS en codage UCS-2, qui passe de 160 a 70 caracteres par segment. Le
message coute alors le double a chaque transfert.

Le montant est formate ici avec une espace ORDINAIRE, pas l'espace fine
insecable du site : cette derniere n'existe pas dans le jeu GSM-7 et
suffirait a elle seule a faire basculer le message.

gettext et non gettext_lazy : le texte est rendu dans une tache Celery,
sous translation.override(langue du client).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.utils.translation import gettext as _

from .models import Template


def format_amount(value) -> str:
    """50000 -> « 50 000,00 », avec une espace ordinaire (GSM-7)."""
    q = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{q:,.2f}".replace(",", " ").replace(".", ",")


def transfer_completed(txn) -> str:
    return _("Plip-Plip : %(amount)s HTG ont ete envoyes au %(phone)s. Reference %(reference)s.") % {
        "amount": format_amount(txn.net_amount),
        "phone": txn.recipient_phone,
        "reference": txn.reference,
    }


def transfer_refunded(txn) -> str:
    return _("Plip-Plip : votre transfert %(reference)s a ete rembourse. %(amount)s HTG vous ont ete rendus.") % {
        "reference": txn.reference,
        "amount": format_amount(txn.net_amount),
    }


def claim_answered(txn) -> str:
    """Avis, pas contenu : le SMS dit qu'une reponse existe et renvoie au
    site. Rien de ce que l'operateur a ecrit ne passe par ici."""
    return _("Plip-Plip : nous avons repondu a votre reclamation sur le transfert %(reference)s. Ouvrez plip.ht pour la lire.") % {
        "reference": txn.reference,
    }


RENDERERS = {
    Template.TRANSFER_COMPLETED: transfer_completed,
    Template.TRANSFER_REFUNDED: transfer_refunded,
    Template.CLAIM_ANSWERED: claim_answered,
}
