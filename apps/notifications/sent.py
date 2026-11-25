"""Adaptateur Sent : la seule porte entre les notifications et Sent.

Un seul gabarit par avis, sur tous les canaux : Sent choisit le canal
(WhatsApp, SMS) et fait son propre repli. Les textes sont ceux des
modeles WhatsApp (whatsapp.TEMPLATES), avec les memes variables, et
doivent etre soumis a Sent mot pour mot.
"""

from __future__ import annotations

from django.conf import settings

from apps.accounts.phone import to_e164
from apps.providers.sent import client as sent_client

from . import whatsapp

#: Langue des gabarits : celle des modeles WhatsApp.
LANGUAGE = whatsapp.LANGUAGE


class SentNotConfigured(Exception):
    """Cle ou gabarit absent : rien n'est tente, rien n'est rejoue."""


def render_body(template: str, txn) -> str:
    return whatsapp.render_body(template, txn)


def parameters(template: str, txn) -> dict[str, str]:
    """{{1}} devient var_1 : convention de nommage des variables chez Sent."""
    return {f"var_{key}": value for key, value in whatsapp.variables(template, txn).items()}


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
