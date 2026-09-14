"""Exceptions du connecteur plopplop.

La distinction centrale n'est pas "erreur technique / erreur metier"
mais : sait-on avec certitude si l'argent a bouge ?

  - PlopPlopError et ses sous-classes  -> l'API a repondu, l'etat est connu.
  - PlopPlopIndeterminate              -> timeout / reseau / 5xx, l'etat
                                          est INCONNU. Interdiction de
                                          rejouer sans verification.
"""

from __future__ import annotations


class PlopPlopError(Exception):
    """Erreur renvoyee explicitement par plopplop."""

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.payload = payload or {}

    def __str__(self) -> str:  # pragma: no cover - confort de debug
        return f"[{self.code or self.status}] {self.message}"


class PlopPlopIndeterminate(PlopPlopError):
    """L'issue de l'appel est inconnue.

    Levee sur timeout, erreur reseau ou 5xx pendant une operation
    d'ecriture. Le decaissement a PEUT-ETRE ete execute cote operateur.
    Le seul traitement autorise est un appel a withdrawal_status() sur
    la reference, jamais un nouvel essai direct.
    """


class AuthenticationError(PlopPlopError):
    """client_id / client_secret invalides, ou compte inactif."""


class InvalidSignature(PlopPlopError):
    """INVALID_SIGNATURE ou NO_CLIENT_SECRET."""


class TimestampExpired(PlopPlopError):
    """TIMESTAMP_EXPIRED : horloge locale desynchronisee de plus de 5 min."""


class InsufficientBalance(PlopPlopError):
    """INSUFFICIENT_BALANCE : le solde prepaye marchand est epuise."""


class DuplicateReference(PlopPlopError):
    """DUPLICATE_REFERENCE (HTTP 409) : reference deja utilisee.

    A traiter comme un signal, pas comme un echec : un retrait porte
    deja cette reference. Verifier son statut avant toute conclusion.
    """


class WithdrawalCooldown(PlopPlopError):
    """WITHDRAWAL_COOLDOWN / HTTP 429 : moins de 120 s depuis le dernier retrait."""


class TransferFailed(PlopPlopError):
    """API_TRANSFER_FAILED : MonCash ou NatCash a rejete le transfert."""


class TokenError(PlopPlopError):
    """INVALID_TOKEN_TYPE, TOKEN_ALREADY_USED, PARAMETER_MISMATCH."""


class MethodNotConfigured(PlopPlopError):
    """METHOD_NOT_CONFIGURED : moyen de paiement inactif cote plopplop."""


ERROR_CODE_MAP: dict[str, type[PlopPlopError]] = {
    "INVALID_SIGNATURE": InvalidSignature,
    "NO_CLIENT_SECRET": InvalidSignature,
    "TIMESTAMP_EXPIRED": TimestampExpired,
    "INSUFFICIENT_BALANCE": InsufficientBalance,
    "DUPLICATE_REFERENCE": DuplicateReference,
    "WITHDRAWAL_COOLDOWN": WithdrawalCooldown,
    "API_TRANSFER_FAILED": TransferFailed,
    "INVALID_TOKEN_TYPE": TokenError,
    "TOKEN_ALREADY_USED": TokenError,
    "PARAMETER_MISMATCH": TokenError,
    "METHOD_NOT_CONFIGURED": MethodNotConfigured,
}


def from_response(status: int, payload: dict) -> PlopPlopError:
    code = payload.get("error_code")
    message = payload.get("message") or f"HTTP {status}"
    cls = ERROR_CODE_MAP.get(code or "")
    if cls is None:
        if status in (401, 403):
            cls = AuthenticationError
        elif status == 409:
            cls = DuplicateReference
        elif status == 429:
            cls = WithdrawalCooldown
        else:
            cls = PlopPlopError
    return cls(message, code=code, status=status, payload=payload)
