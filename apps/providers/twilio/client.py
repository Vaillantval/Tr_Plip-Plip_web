"""Client Twilio Verify.

Seul module autorise a parler a Twilio. Twilio Verify genere, expedie et
verifie lui-meme les codes : Plip-Plip ne stocke aucun code en production.
"""

from __future__ import annotations

import logging

import requests

from .exceptions import TwilioUnavailable, from_response

logger = logging.getLogger(__name__)

BASE_URL = "https://verify.twilio.com/v2"


class TwilioVerifyClient:
    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        service_sid: str,
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.account_sid = account_sid
        self._auth_token = auth_token
        self.service_sid = service_sid
        self.timeout = timeout
        self._session = session or requests.Session()

    def _post(self, path: str, data: dict) -> dict:
        url = f"{BASE_URL}/Services/{self.service_sid}/{path}"
        try:
            response = self._session.post(
                url, data=data, auth=(self.account_sid, self._auth_token), timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise TwilioUnavailable(f"Aucune reponse de Twilio ({path}) : {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            raise from_response(response.status_code, payload)
        return payload

    def start_verification(self, *, to: str, channel: str = "sms") -> str:
        """Envoie un code. `to` au format E.164 (+509XXXXXXXX). Retourne le statut."""
        return self._post("Verifications", {"To": to, "Channel": channel}).get("status", "")

    def check_verification(self, *, to: str, code: str) -> bool:
        """True seulement si Twilio repond 'approved'."""
        payload = self._post("VerificationCheck", {"To": to, "Code": code})
        return payload.get("status") == "approved"


def get_client() -> TwilioVerifyClient:
    from django.conf import settings

    conf = settings.TWILIO
    return TwilioVerifyClient(
        account_sid=conf["ACCOUNT_SID"],
        auth_token=conf["AUTH_TOKEN"],
        service_sid=conf["VERIFY_SERVICE_SID"],
        timeout=conf["TIMEOUT"],
    )
