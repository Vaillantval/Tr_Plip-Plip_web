"""Client Sent : envoi de messages a partir de gabarits approuves.

Sent choisit lui-meme le canal (WhatsApp, SMS...) et fait son propre
repli : nous ne fournissons que le destinataire, le gabarit et ses
variables.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .exceptions import SentUnavailable, from_response

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sent.dm/v3"


@dataclass(frozen=True)
class SentMessage:
    id: str
    status: str


def _first_message(data) -> dict:
    """La reponse 202 porte le message dans data, seul ou en liste."""
    if isinstance(data, dict):
        for key in ("messages", "recipients", "items"):
            items = data.get(key)
            if isinstance(items, list) and items and isinstance(items[0], dict):
                return items[0]
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return {}


class SentClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout: float = 10.0,
        sandbox: bool = False,
        session: requests.Session | None = None,
    ):
        self._api_key = api_key
        self.timeout = timeout
        self.sandbox = sandbox
        self._session = session or requests.Session()

    def send_template(
        self, *, to: str, template_id: str, parameters: dict[str, str], idempotency_key: str = ""
    ) -> SentMessage:
        """Envoie un gabarit. `to` au format E.164 (+509XXXXXXXX).

        `idempotency_key` : Sent rejoue la reponse d'origine pendant 24 h
        pour la meme cle et le meme corps -- une reprise ne double pas le
        message.

        Leve SentInvalidPhone / SentRateLimited / SentUnavailable /
        SentError -- jamais une exception requests.
        """
        body = {"to": [to], "template": {"id": template_id, "parameters": parameters}}
        if self.sandbox:
            body["sandbox"] = True
        headers = {"x-api-key": self._api_key, "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            response = self._session.post(f"{BASE_URL}/messages", json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise SentUnavailable(f"Aucune reponse de Sent : {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            raise from_response(response.status_code, payload)
        message = _first_message(payload.get("data") if isinstance(payload, dict) else None)
        message_id = str(message.get("id") or message.get("message_id") or message.get("messageId") or "")
        status = str(message.get("status") or "QUEUED")
        return SentMessage(id=message_id, status=status)


def get_client() -> SentClient:
    from django.conf import settings

    conf = settings.SENT
    return SentClient(api_key=conf["API_KEY"], timeout=conf["TIMEOUT"], sandbox=conf["SANDBOX"])


def sent_configured() -> bool:
    from django.conf import settings

    return bool(settings.SENT["API_KEY"])


def otp_ready() -> bool:
    from django.conf import settings

    return sent_configured() and bool(settings.SENT["TEMPLATES"].get("otp"))


#: Gabarits des avis, dans l'ordre de apps.notifications.models.Template.
NOTIFICATION_TEMPLATES = ("transfer_completed", "transfer_refunded", "claim_answered")


def notifications_ready() -> bool:
    from django.conf import settings

    templates = settings.SENT["TEMPLATES"]
    return sent_configured() and all(templates.get(name) for name in NOTIFICATION_TEMPLATES)


def missing_for_otp() -> list[str]:
    from django.conf import settings

    missing = [] if sent_configured() else ["SENT_API_KEY"]
    if not settings.SENT["TEMPLATES"].get("otp"):
        missing.append("SENT_TEMPLATE_OTP")
    return missing


def missing_for_notifications() -> list[str]:
    from django.conf import settings

    missing = [] if sent_configured() else ["SENT_API_KEY"]
    templates = settings.SENT["TEMPLATES"]
    missing += [f"SENT_TEMPLATE_{name.upper()}" for name in NOTIFICATION_TEMPLATES if not templates.get(name)]
    return missing
