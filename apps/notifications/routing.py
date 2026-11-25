"""Quel fournisseur envoie quoi, et chacun est-il pret.

Le choix vient de MessagingSetting (console, superadmin). Les fonctions
*_missing() rendent les NOMS des variables d'environnement manquantes,
jamais leurs valeurs : la console les affiche.
"""

from __future__ import annotations

from django.conf import settings

from apps.providers.sent import client as sent

from .models import MessagingSetting, Provider


def otp_provider() -> str:
    return MessagingSetting.current().otp_provider


def notification_provider() -> str:
    return MessagingSetting.current().notification_provider


def otp_missing(provider: str) -> list[str]:
    if provider == Provider.SENT:
        return sent.missing_for_otp()
    conf = settings.TWILIO
    names = {"ACCOUNT_SID": "TWILIO_ACCOUNT_SID", "AUTH_TOKEN": "TWILIO_AUTH_TOKEN",
             "VERIFY_SERVICE_SID": "TWILIO_VERIFY_SERVICE_SID"}
    return [env for key, env in names.items() if not conf[key]]


def notifications_missing(provider: str) -> list[str]:
    if provider == Provider.SENT:
        return sent.missing_for_notifications()
    conf = settings.TWILIO
    missing = [env for key, env in (("ACCOUNT_SID", "TWILIO_ACCOUNT_SID"), ("AUTH_TOKEN", "TWILIO_AUTH_TOKEN"))
               if not conf[key]]
    if not (conf["MESSAGING_SERVICE_SID"] or conf["FROM_NUMBER"]):
        missing.append("TWILIO_MESSAGING_SERVICE_SID")
    return missing


def status() -> dict:
    """Etat complet, pour la console."""
    current = MessagingSetting.current()
    return {
        "setting": MessagingSetting.objects.filter(pk=1).select_related("updated_by").first(),
        "otp_provider": current.otp_provider,
        "notification_provider": current.notification_provider,
        "providers": [
            {
                "value": value,
                "label": label,
                "otp_missing": otp_missing(value),
                "notifications_missing": notifications_missing(value),
            }
            for value, label in Provider.choices
        ],
    }


class ProviderNotReady(Exception):
    """Bascule refusee : le fournisseur vise n'est pas configure."""


def choose(*, otp_provider: str, notification_provider: str, actor) -> MessagingSetting:
    """Change les fournisseurs actifs. Leve ProviderNotReady, ValueError.

    Passer sur Sent exige qu'il soit pret : sinon plus aucun client ne
    pourrait se connecter. Revenir sur Twilio est toujours permis -- c'est
    l'etat par defaut, celui d'avant toute bascule.
    """
    valid = set(Provider.values)
    if otp_provider not in valid or notification_provider not in valid:
        raise ValueError("Fournisseur inconnu")
    current = MessagingSetting.current()
    if otp_provider == Provider.SENT and otp_provider != current.otp_provider:
        missing = otp_missing(Provider.SENT)
        if missing:
            raise ProviderNotReady(f"Sent n'est pas prêt pour les codes (manque : {', '.join(missing)})")
    if notification_provider == Provider.SENT and notification_provider != current.notification_provider:
        missing = notifications_missing(Provider.SENT)
        if missing:
            raise ProviderNotReady(f"Sent n'est pas prêt pour les avis (manque : {', '.join(missing)})")
    row, _ = MessagingSetting.objects.get_or_create(pk=1)
    row.otp_provider = otp_provider
    row.notification_provider = notification_provider
    row.updated_by = actor
    row.save()
    MessagingSetting.clear_cache()
    return row
