from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

from apps.providers.sent.client import otp_ready as sent_otp_ready

from .otp import twilio_configured


@register(Tags.security, deploy=True)
def otp_backend_is_production_ready(app_configs, **kwargs):
    if settings.OTP["BACKEND"] == "console":
        return [
            Error(
                f"OTP_BACKEND vaut {settings.OTP['BACKEND']!r} : les codes seraient ecrits dans les logs.",
                hint="Utiliser OTP_BACKEND=live en production.",
                id="plipplip.E001",
            )
        ]
    if not twilio_configured() and not sent_otp_ready():
        # Avertissement, pas erreur : le deploiement n'est pas bloque, mais
        # les clients ne peuvent pas se connecter tant qu'aucun fournisseur
        # n'est configure. Lequel est actif se regle dans la console.
        return [
            Warning(
                "Aucun fournisseur de codes configure : l'identification SMS des clients est indisponible.",
                hint=(
                    "Renseigner SENT_API_KEY, ou TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN et "
                    "TWILIO_VERIFY_SERVICE_SID, puis choisir le fournisseur dans la console."
                ),
                id="plipplip.W002",
            )
        ]
    return []
