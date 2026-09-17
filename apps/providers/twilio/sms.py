"""Client Twilio Messages (SMS transactionnels).

Separe de client.py : Verify et Messages sont deux API, deux domaines
(verify.twilio.com / api.twilio.com) et deux identifiants de ressource
(un service Verify VA..., un compte AC...). La regle « un seul module
parle a Twilio » devient « un module par API Twilio », tous deux ici.

Les erreurs sont traduites par le meme from_response : ses codes
couvrent deja Messages (21211, 21614 sont des codes Messages).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from .exceptions import TwilioUnavailable, from_response

logger = logging.getLogger(__name__)

BASE_URL = "https://api.twilio.com/2010-04-01"


@dataclass(frozen=True)
class SentMessage:
    sid: str
    status: str  # queued | sending | sent | delivered | failed | undelivered


class TwilioSMSClient:
    def __init__(
        self,
        *,
        account_sid: str,
        auth_token: str,
        messaging_service_sid: str = "",
        from_number: str = "",
        timeout: float = 10.0,
        session: requests.Session | None = None,
    ):
        self.account_sid = account_sid
        self._auth_token = auth_token
        self.messaging_service_sid = messaging_service_sid
        self.from_number = from_number
        self.timeout = timeout
        self._session = session or requests.Session()

    def send(self, *, to: str, body: str, idempotency_key: str = "") -> SentMessage:
        """Envoie un SMS. `to` au format E.164 (+509XXXXXXXX).

        `idempotency_key` : Twilio dedoublonne cote serveur les envois
        portant la meme cle. C'est ce qui ferme le trou entre l'acceptation
        par Twilio et l'ecriture du statut chez nous -- un client qui
        recevrait deux fois « transfert termine » appellerait le support en
        croyant avoir paye deux fois.

        Leve TwilioInvalidPhone / TwilioRateLimited / TwilioUnavailable /
        TwilioError -- jamais une exception requests.
        """
        data = {"To": to, "Body": body}
        if self.messaging_service_sid:
            data["MessagingServiceSid"] = self.messaging_service_sid
        else:
            data["From"] = self.from_number

        headers = {"I-Twilio-Idempotency-Token": idempotency_key} if idempotency_key else {}
        url = f"{BASE_URL}/Accounts/{self.account_sid}/Messages.json"
        try:
            response = self._session.post(
                url,
                data=data,
                headers=headers,
                auth=(self.account_sid, self._auth_token),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise TwilioUnavailable(f"Aucune reponse de Twilio (Messages) : {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            raise from_response(response.status_code, payload)
        return SentMessage(sid=payload.get("sid", ""), status=payload.get("status", ""))


def get_sms_client() -> TwilioSMSClient:
    from django.conf import settings

    conf = settings.TWILIO
    return TwilioSMSClient(
        account_sid=conf["ACCOUNT_SID"],
        auth_token=conf["AUTH_TOKEN"],
        messaging_service_sid=conf["MESSAGING_SERVICE_SID"],
        from_number=conf["FROM_NUMBER"],
        timeout=conf["TIMEOUT"],
    )
