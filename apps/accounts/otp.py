"""Codes a usage unique (OTP) pour l'identification des clients.

settings.OTP["BACKEND"] :

  - "live"    : production (config/settings/prod.py). Le fournisseur --
                Twilio ou Sent -- est choisi dans la console
                (notifications.MessagingSetting), Twilio par defaut.
                "twilio" est accepte comme synonyme (ancienne valeur).
  - "console" : developpement uniquement. Le code est ecrit dans les logs
                et verifie localement. `manage.py check --deploy` refuse
                ce backend (apps.accounts.checks).

Twilio Verify genere et verifie le code lui-meme. Avec Sent, nous le
generons, le gardons hache en cache et limitons nous-memes les envois.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets

from django.conf import settings
from django.core.cache import cache

from apps.providers.sent import client as sent_client
from apps.providers.sent import exceptions as st
from apps.providers.twilio import exceptions as tw
from apps.providers.twilio.client import get_client

from .phone import to_e164

logger = logging.getLogger(__name__)

CODE_LENGTH = 6
#: Limites d'envoi pour les fournisseurs qui n'en ont pas (Sent).
RESEND_COOLDOWN_SECONDS = 30
MAX_SENDS_PER_HOUR = 5


class OTPError(Exception):
    pass


class OTPRateLimited(OTPError):
    pass


class OTPInvalidPhone(OTPError):
    pass


class OTPUnavailable(OTPError):
    pass


def twilio_configured() -> bool:
    conf = settings.TWILIO
    return all(conf[key] for key in ("ACCOUNT_SID", "AUTH_TOKEN", "VERIFY_SERVICE_SID"))


def generate_code() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(CODE_LENGTH))


class LocalCodeStore:
    """Code genere ici, garde hache en cache, tentatives limitees."""

    def _key(self, phone: str) -> str:
        return f"plipplip:otp:{phone}"

    def _digest(self, phone: str, code: str) -> str:
        return hmac.new(settings.SECRET_KEY.encode(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()

    def issue(self, phone: str) -> str:
        code = generate_code()
        entry = {"digest": self._digest(phone, code), "attempts": 0}
        cache.set(self._key(phone), entry, settings.OTP["CODE_TTL_SECONDS"])
        return code

    def discard(self, phone: str) -> None:
        cache.delete(self._key(phone))

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


class ConsoleOTPBackend:
    """Code genere localement, garde haché en cache, ecrit dans les logs."""

    def __init__(self):
        self.store = LocalCodeStore()

    def send(self, phone: str) -> None:
        code = self.store.issue(phone)
        logger.warning("Code OTP (backend console, DEV UNIQUEMENT) pour %s : %s", phone, code)

    def check(self, phone: str, code: str) -> bool:
        return self.store.check(phone, code)


class TwilioOTPBackend:
    @staticmethod
    def _ensure_configured() -> None:
        # Sans identifiants, aucun appel a Twilio : le client voit « envoi
        # momentanement impossible », le reste de la plateforme tourne.
        if not twilio_configured():
            logger.error("Twilio non configure : aucun code SMS ne peut etre envoye ni verifie")
            raise OTPUnavailable("Twilio non configure")

    def send(self, phone: str) -> None:
        self._ensure_configured()
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
        self._ensure_configured()
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


class SentOTPBackend:
    """Code genere ici, expedie par Sent avec le gabarit d'authentification."""

    def __init__(self):
        self.store = LocalCodeStore()

    @staticmethod
    def _throttle(phone: str) -> None:
        # Twilio Verify limite seul ; Sent non. Sans cette borne, un robot
        # viderait le solde Sent en demandant des codes en boucle.
        if not cache.add(f"plipplip:otp:cooldown:{phone}", 1, RESEND_COOLDOWN_SECONDS):
            raise OTPRateLimited("Un code vient d'etre envoye : patienter avant d'en demander un autre")
        hour_key = f"plipplip:otp:hour:{phone}"
        cache.add(hour_key, 0, 3600)
        try:
            count = cache.incr(hour_key)
        except ValueError:  # cle expiree entre add et incr
            cache.set(hour_key, 1, 3600)
            count = 1
        if count > MAX_SENDS_PER_HOUR:
            raise OTPRateLimited("Trop de codes demandes : reessayer plus tard")

    def send(self, phone: str) -> None:
        if not sent_client.otp_ready():
            logger.error("Sent non configure : aucun code ne peut etre envoye")
            raise OTPUnavailable("Sent non configure")
        self._throttle(phone)
        code = self.store.issue(phone)
        conf = settings.SENT
        try:
            sent_client.get_client().send_template(
                to=to_e164(phone),
                template_id=conf["TEMPLATES"]["otp"],
                parameters={conf["OTP_VARIABLE"]: code},
                idempotency_key=f"plipplip-otp-{secrets.token_hex(12)}",
            )
        except st.SentError as exc:
            # Code jamais parti : il ne doit pas rester valable.
            self.store.discard(phone)
            if isinstance(exc, st.SentRateLimited):
                raise OTPRateLimited(str(exc)) from exc
            if isinstance(exc, st.SentInvalidPhone):
                raise OTPInvalidPhone(str(exc)) from exc
            logger.error("Envoi OTP Sent impossible vers %s : %s", phone, exc)
            raise OTPUnavailable(str(exc)) from exc

    def check(self, phone: str, code: str) -> bool:
        return self.store.check(phone, code)


PROVIDER_BACKENDS = {"twilio": TwilioOTPBackend, "sent": SentOTPBackend}


class LiveOTPBackend:
    """Aiguille vers le fournisseur choisi dans la console.

    Le fournisseur qui a envoye le code est retenu pour ce numero : un
    client qui a recu un code Twilio peut le saisir meme si le superadmin
    vient de passer sur Sent.
    """

    @staticmethod
    def _marker(phone: str) -> str:
        return f"plipplip:otp:provider:{phone}"

    @staticmethod
    def _current() -> str:
        from apps.notifications.routing import otp_provider

        return otp_provider()

    def send(self, phone: str) -> None:
        provider = self._current()
        PROVIDER_BACKENDS[provider]().send(phone)
        cache.set(self._marker(phone), provider, settings.OTP["CODE_TTL_SECONDS"])

    def check(self, phone: str, code: str) -> bool:
        provider = cache.get(self._marker(phone)) or self._current()
        ok = PROVIDER_BACKENDS.get(provider, TwilioOTPBackend)().check(phone, code)
        if ok:
            cache.delete(self._marker(phone))
        return ok


BACKENDS = {"console": ConsoleOTPBackend, "live": LiveOTPBackend, "twilio": LiveOTPBackend}


def get_backend():
    name = settings.OTP["BACKEND"]
    try:
        return BACKENDS[name]()
    except KeyError as exc:
        raise OTPError(f"Backend OTP inconnu : {name}") from exc
