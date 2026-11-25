"""Adaptateur Sent : la seule porte entre les notifications et Sent.

Un seul gabarit par avis, sur tous les canaux : Sent choisit le canal
(WhatsApp, SMS) et fait son propre repli. Le texte part donc AUSSI par
SMS : il est SANS ACCENT, comme les SMS Twilio (GSM-7, 160 caracteres,
sinon le message coute le double).

Les textes ci-dessous sont ceux soumis a Sent, mot pour mot. Les modifier
sans faire approuver un nouveau gabarit ne change rien a ce que recoit le
client, mais fausse ce que lit le support dans Notification.body.

En francais uniquement : le creole n'est pas une langue de gabarit
WhatsApp.
"""

from __future__ import annotations

from django.conf import settings

from apps.accounts.phone import to_e164
from apps.providers.sent import client as sent_client

from .messages import format_amount
from .models import Template

LANGUAGE = "fr"

TEMPLATES = {
    Template.TRANSFER_COMPLETED: (
        "Plip-Plip : {{amount}} HTG ont ete envoyes au {{phone}}. Reference {{reference}}. Merci de votre confiance."
    ),
    Template.TRANSFER_REFUNDED: (
        "Plip-Plip : votre transfert {{reference}} a ete rembourse. {{amount}} HTG vous ont ete rendus."
    ),
    #: Avis, pas contenu : rien de la reponse ne passe ici.
    Template.CLAIM_ANSWERED: (
        "Plip-Plip : nous avons repondu a votre reclamation sur le transfert {{reference}}. "
        "Ouvrez plip.ht pour la lire."
    ),
}


class SentNotConfigured(Exception):
    """Cle ou gabarit absent : rien n'est tente, rien n'est rejoue."""


def parameters(template: str, txn) -> dict[str, str]:
    if template == Template.TRANSFER_COMPLETED:
        return {"amount": format_amount(txn.net_amount), "phone": txn.recipient_phone, "reference": txn.reference}
    if template == Template.TRANSFER_REFUNDED:
        return {"reference": txn.reference, "amount": format_amount(txn.net_amount)}
    if template == Template.CLAIM_ANSWERED:
        return {"reference": txn.reference}
    raise ValueError(f"Gabarit Sent inconnu : {template}")


def render_body(template: str, txn) -> str:
    """Le texte que recevra le client, pour le journal du support."""
    body = TEMPLATES[template]
    for key, value in parameters(template, txn).items():
        body = body.replace("{{%s}}" % key, value)
    return body


def send(*, phone: str, template: str, txn, idempotency_key: str) -> tuple[str, str]:
    """Envoie l'avis. Retourne (identifiant Sent, statut).

    Leve SentNotConfigured, ou une exception Sent typee.
    """
    template_id = settings.SENT["TEMPLATES"].get(template)
    if not sent_client.sent_configured() or not template_id:
        raise SentNotConfigured(f"Sent non configure pour {template}")
    sent = sent_client.get_client().send_template(
        to=to_e164(phone),
        template_id=template_id,
        parameters=parameters(template, txn),
        idempotency_key=idempotency_key,
    )
    return sent.id, sent.status
