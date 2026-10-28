from __future__ import annotations

from django.core.checks import Tags, Warning, register

from django.conf import settings

from .sms import sms_configured
from .whatsapp import whatsapp_configured


@register(Tags.security, deploy=True)
def transactional_sms_is_configured(app_configs, **kwargs):
    if not sms_configured():
        # Avertissement, jamais erreur : la plateforme fonctionne sans SMS
        # de notification, elle ne doit pas refuser de se deployer.
        return [
            Warning(
                "Expediteur SMS absent : aucune notification de transfert ne partira.",
                hint="Renseigner TWILIO_MESSAGING_SERVICE_SID (ou, a defaut, TWILIO_FROM_NUMBER).",
                id="plipplip.W003",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def whatsapp_is_fully_configured_or_not_at_all(app_configs, **kwargs):
    conf = settings.TWILIO
    started = conf["WHATSAPP_FROM"] or conf["STATUS_CALLBACK_URL"] or any(conf["WHATSAPP_CONTENT_SIDS"].values())
    if started and not whatsapp_configured():
        # Le piege : on croit WhatsApp ouvert, mais la page ne le propose
        # pas et tout part en SMS.
        return [
            Warning(
                "WhatsApp configure a moitie : le choix n'est pas propose aux clients, tout part en SMS.",
                hint=(
                    "Renseigner TWILIO_WHATSAPP_FROM, TWILIO_STATUS_CALLBACK_URL et les trois "
                    "TWILIO_WA_*_SID, ou n'en renseigner aucun."
                ),
                id="plipplip.W004",
            )
        ]
    return []
