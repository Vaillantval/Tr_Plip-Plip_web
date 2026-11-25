"""Exceptions du connecteur Sent."""

from __future__ import annotations


class SentError(Exception):
    def __init__(self, message: str, *, code: str | None = None, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status

    def __str__(self) -> str:  # pragma: no cover - confort de debug
        return f"[{self.code or self.status}] {self.message}"


class SentUnavailable(SentError):
    """Timeout, erreur reseau, 5xx, ou doublon encore en cours (409)."""


class SentRateLimited(SentError):
    """429 : plafond d'appels atteint."""


class SentInvalidPhone(SentError):
    """Numero refuse par Sent (champ `to` invalide)."""


def from_response(status: int, payload: dict) -> SentError:
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    message = error.get("message") or f"HTTP {status}"
    details = error.get("details") or {}
    if status >= 500 or status == 409:
        return SentUnavailable(message, code=code, status=status)
    if status == 429:
        return SentRateLimited(message, code=code, status=status)
    if status == 400 and isinstance(details, dict) and any(str(k).startswith("to") for k in details):
        return SentInvalidPhone(message, code=code, status=status)
    return SentError(message, code=code, status=status)
