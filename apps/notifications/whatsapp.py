"""Adaptateur WhatsApp : la seule porte entre les notifications et Twilio WhatsApp.

Les textes ci-dessous sont ceux des modeles a soumettre a Meta, mot pour
mot. Meta n'envoie que le texte qu'il a approuve : nous ne fournissons
que les variables {{1}}, {{2}}... Modifier un texte ici sans faire
approuver le nouveau modele ne change rien a ce que recoit le client,
mais fausse ce que lit le support dans Notification.body.

En FRANCAIS, et pas en creole : le creole haitien n'est pas une langue de
modele WhatsApp, et un texte qui ne correspond pas a la langue declaree
est rejete (TAG_CONTENT_MISMATCH).

Les accents sont permis : la regle « SMS sans accent » tient au codage
GSM-7 des SMS, que WhatsApp n'utilise pas.

Aucun modele ne commence ni ne finit par une variable : Meta les refuse.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apps.accounts.phone import to_e164
from apps.providers.twilio.sms import get_sms_client

from .messages import format_amount
from .models import Template

logger = logging.getLogger(__name__)

#: Langue declaree a Meta pour les trois modeles.
LANGUAGE = "fr"

TEMPLATES = {
    Template.TRANSFER_COMPLETED: (
        "Plip-Plip : {{1}} HTG ont été envoyés au {{2}}. Référence : {{3}}. Merci de votre confiance."
    ),
    Template.TRANSFER_REFUNDED: (
        "Plip-Plip : votre transfert {{1}} a été remboursé. {{2}} HTG vous ont été rendus."
    ),
    #: Avis, pas contenu : comme le SMS, rien de la reponse ne passe ici.
    #: Un apercu WhatsApp s'affiche lui aussi sur un ecran verrouille.
    Template.CLAIM_ANSWERED: (
        "Plip-Plip : nous avons répondu à votre réclamation sur le transfert {{1}}. Ouvrez plip.ht pour la lire."
    ),
}


def variables(template: str, txn) -> dict[str, str]:
    if template == Template.TRANSFER_COMPLETED:
        return {"1": format_amount(txn.net_amount), "2": txn.recipient_phone, "3": txn.reference}
    if template == Template.TRANSFER_REFUNDED:
        return {"1": txn.reference, "2": format_amount(txn.net_amount)}
    if template == Template.CLAIM_ANSWERED:
        return {"1": txn.reference}
    raise ValueError(f"Modele WhatsApp inconnu : {template}")


def render_body(template: str, txn) -> str:
    """Le texte que Meta affichera, pour le journal du support."""
    body = TEMPLATES[template]
    for key, value in variables(template, txn).items():
        body = body.replace("{{%s}}" % key, value)
    return body


class WhatsAppNotConfigured(Exception):
    """Un reglage manque : l'avis part en SMS."""


def whatsapp_configured() -> bool:
    """Tout ou rien : un seul modele manquant et l'avis partirait sans
    contenu. Tant que c'est faux, le choix WhatsApp n'est pas propose."""
    conf = settings.TWILIO
    return bool(
        conf["ACCOUNT_SID"]
        and conf["AUTH_TOKEN"]
        and conf["WHATSAPP_FROM"]
        and conf["STATUS_CALLBACK_URL"]
        and all(conf["WHATSAPP_CONTENT_SIDS"].get(t) for t in TEMPLATES)
    )


def send(*, phone: str, template: str, txn, notification_id: int, idempotency_key: str) -> tuple[str, str]:
    """Envoie l'avis. Retourne (identifiant Twilio, statut).

    Leve WhatsAppNotConfigured, ou une exception Twilio typee.
    """
    if not whatsapp_configured():
        raise WhatsAppNotConfigured("WhatsApp non configure")
    conf = settings.TWILIO
    sent = get_sms_client().send_whatsapp(
        to=to_e164(phone),
        from_number=conf["WHATSAPP_FROM"],
        content_sid=conf["WHATSAPP_CONTENT_SIDS"][template],
        variables=variables(template, txn),
        # L'identifiant de la ligne voyage dans l'URL : le statut d'echec
        # peut arriver avant meme que nous ayons ecrit l'identifiant Twilio.
        status_callback=f"{conf['STATUS_CALLBACK_URL']}?n={notification_id}",
        idempotency_key=idempotency_key,
    )
    return sent.sid, sent.status
