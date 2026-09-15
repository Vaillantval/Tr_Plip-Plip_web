from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

from .otp import twilio_configured


@register(Tags.security, deploy=True)
def otp_backend_is_production_ready(app_configs, **kwargs):
    if settings.OTP["BACKEND"] != "twilio":
        return [
            Error(
                f"OTP_BACKEND vaut {settings.OTP['BACKEND']!r} : les codes seraient ecrits dans les logs.",
                hint="Utiliser OTP_BACKEND=twilio en production.",
                id="plipplip.E001",
            )
        ]
    if not twilio_configured():
        # Avertissement, pas erreur : le deploiement n'est pas bloque, mais
        # les clients ne peuvent pas se connecter tant que Twilio manque.
        return [
            Warning(
                "Identifiants Twilio incomplets : l'identification SMS des clients est indisponible.",
                hint="Renseigner TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN et TWILIO_VERIFY_SERVICE_SID.",
                id="plipplip.W002",
            )
        ]
    return []
