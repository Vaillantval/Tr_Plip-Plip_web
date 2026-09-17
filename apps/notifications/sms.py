"""Adaptateur SMS : la seule porte entre les notifications et Twilio."""

from __future__ import annotations

import logging

from django.conf import settings

from apps.accounts.phone import to_e164
from apps.providers.twilio import exceptions as tw
from apps.providers.twilio.sms import get_sms_client

logger = logging.getLogger(__name__)


class SMSNotConfigured(Exception):
    """Aucun expediteur : rien n'est tente, rien n'est rejoue."""


def sms_configured() -> bool:
    """Independant de otp.twilio_configured().

    Les identifiants sont communs, mais pas les consequences : sans
    VERIFY_SERVICE_SID les clients ne peuvent pas se connecter, sans
    expediteur ils transigent mais ne recoivent aucune notification. Deux
    pannes, deux variables, deux avertissements distincts.
    """
    conf = settings.TWILIO
    return bool(
        conf["ACCOUNT_SID"]
        and conf["AUTH_TOKEN"]
        and (conf["MESSAGING_SERVICE_SID"] or conf["FROM_NUMBER"])
    )


def send(*, phone: str, body: str, idempotency_key: str) -> tuple[str, str]:
    """Envoie un SMS. Retourne (identifiant Twilio, statut).

    Leve SMSNotConfigured, ou une exception Twilio typee.
    """
    if not sms_configured():
        logger.warning("Expediteur SMS Twilio absent : aucune notification ne part")
        raise SMSNotConfigured("Expediteur SMS non configure")
    try:
        sent = get_sms_client().send(to=to_e164(phone), body=body, idempotency_key=idempotency_key)
    except tw.TwilioError:
        raise
    return sent.sid, sent.status
