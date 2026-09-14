"""Codes a usage unique (OTP) pour l'identification des clients.

Deux backends, choisis par settings.OTP["BACKEND"] :

  - "twilio"  : Twilio Verify envoie et verifie le code. Obligatoire en
                production (config/settings/prod.py).
  - "console" : developpement uniquement. Le code est ecrit dans les logs
                et verifie localement. `manage.py check --deploy` refuse
                ce backend (apps.accounts.checks).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from django.conf import settings
from django.core.cache import cache

from apps.providers.twilio import exceptions as tw
from apps.providers.twilio.client import get_client

from .phone import to_e164

logger = logging.getLogger(__name__)

CODE_LENGTH = 6


class OTPError(Exception):
    pass


class OTPRateLimited(OTPError):
    pass


class OTPInvalidPhone(OTPError):
    pass


class OTPUnavailable(OTPError):
    pass


def generate_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))


class ConsoleOTPBackend:
    """Code genere localement, garde haché en cache, ecrit dans les logs."""

    def _key(self, phone: str) -> str:
        return f"plipplip:otp:{phone}"

    def _digest(self, phone: str, code: str) -> str:
        return hmac.new(settings.SECRET_KEY.encode(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()

    def send(self, phone: str) -> None:
        code = generate_code()
        entry = {"digest": self._digest(phone, code), "attempts": 0}
        cache.set(self._key(phone), entry, settings.OTP["CODE_TTL_SECONDS"])
        logger.warning("Code OTP (backend console, DEV UNIQUEMENT) pour %s : %s", phone, code)

    def check(self, phone: str, code: str) -> bool:
        entry = cache.get(self._key(phone))
        if entry is None:
            return False
        if entry["attempts"] >= settings.OTP["MAX_CHECK_ATTEMPTS"]:
            cache.delete(self._key(phone))
            raise OTPRateLimited("Trop de tentatives : demander un nouveau code")
        if hmac.compare_digest(entry["digest"], self._digest(phone, code)):
            cache.delete(self._key(phone))
            return True
        entry["attempts"] += 1
        cache.set(self._key(phone), entry, settings.OTP["CODE_TTL_SECONDS"])
        return False


class TwilioOTPBackend:
    def send(self, phone: str) -> None:
        try:
            get_client().start_verification(to=to_e164(phone))
        except tw.TwilioRateLimited as exc:
            raise OTPRateLimited(str(exc)) from exc
        except tw.TwilioInvalidPhone as exc:
            raise OTPInvalidPhone(str(exc)) from exc
        except tw.TwilioError as exc:
            logger.error("Envoi OTP impossible vers %s : %s", phone, exc)
            raise OTPUnavailable(str(exc)) from exc

    def check(self, phone: str, code: str) -> bool:
        try:
            return get_client().check_verification(to=to_e164(phone), code=code)
        except tw.VerificationNotFound:
            # Code expire, deja utilise ou tentatives epuisees cote Twilio.
            return False
        except tw.TwilioRateLimited as exc:
            raise OTPRateLimited(str(exc)) from exc
        except tw.TwilioError as exc:
            logger.error("Verification OTP impossible pour %s : %s", phone, exc)
            raise OTPUnavailable(str(exc)) from exc


BACKENDS = {"console": ConsoleOTPBackend, "twilio": TwilioOTPBackend}


def get_backend():
    name = settings.OTP["BACKEND"]
    try:
        return BACKENDS[name]()
    except KeyError as exc:
        raise OTPError(f"Backend OTP inconnu : {name}") from exc
