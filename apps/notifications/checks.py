from __future__ import annotations

from django.core.checks import Tags, Warning, register

from .sms import sms_configured


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
