from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def otp_backend_is_production_ready(app_configs, **kwargs):
    errors = []
    if settings.OTP["BACKEND"] != "twilio":
        errors.append(
            Error(
                f"OTP_BACKEND vaut {settings.OTP['BACKEND']!r} : les codes seraient ecrits dans les logs.",
                hint="Utiliser OTP_BACKEND=twilio en production.",
                id="plipplip.E001",
            )
        )
    elif not all(settings.TWILIO[k] for k in ("ACCOUNT_SID", "AUTH_TOKEN", "VERIFY_SERVICE_SID")):
        errors.append(
            Error(
                "Identifiants Twilio incomplets.",
                hint="Renseigner TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN et TWILIO_VERIFY_SERVICE_SID.",
                id="plipplip.E002",
            )
        )
    return errors
