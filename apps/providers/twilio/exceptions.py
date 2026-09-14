"""Exceptions du connecteur Twilio Verify."""

from __future__ import annotations

#: Codes Twilio signalant un plafond d'envoi ou de verification atteint.
RATE_LIMIT_CODES = frozenset({20429, 60202, 60203})
#: Codes Twilio signalant un numero inutilisable.
INVALID_PHONE_CODES = frozenset({21211, 21614, 60200, 60205})


class TwilioError(Exception):
    def __init__(self, message: str, *, code: int | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status

    def __str__(self) -> str:  # pragma: no cover - confort de debug
        return f"[{self.code or self.status}] {self.message}"


class TwilioUnavailable(TwilioError):
    """Timeout, erreur reseau ou 5xx : aucun code n'a pu etre envoye ou verifie."""


class TwilioRateLimited(TwilioError):
    """Plafond d'envoi ou de verification atteint pour ce numero."""


class TwilioInvalidPhone(TwilioError):
    """Numero refuse par Twilio."""


class VerificationNotFound(TwilioError):
    """Aucune verification en cours : code expire, deja valide ou epuise."""


def from_response(status: int, payload: dict) -> TwilioError:
    code = payload.get("code")
    message = payload.get("message") or f"HTTP {status}"
    if status >= 500:
        return TwilioUnavailable(message, code=code, status=status)
    if status == 429 or code in RATE_LIMIT_CODES:
        return TwilioRateLimited(message, code=code, status=status)
    if code in INVALID_PHONE_CODES:
        return TwilioInvalidPhone(message, code=code, status=status)
    if status == 404:
        return VerificationNotFound(message, code=code, status=status)
    return TwilioError(message, code=code, status=status)
