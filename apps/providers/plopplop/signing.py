"""Signature HMAC-SHA256 pour l'API de retrait plopplop.

Formule imposee par la doc v1.4 :

    HMAC-SHA256("amount|method|recipient|reference|timestamp", client_secret)

L'ordre et le separateur sont stricts. Le serveur distant recalcule la
signature a partir des memes champs : toute divergence de formatage
(notamment sur le montant) produit un INVALID_SIGNATURE.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal

SIGNATURE_FIELD_ORDER = ("amount", "method", "recipient", "reference", "timestamp")


def format_amount(amount: Decimal | int | float | str) -> str:
    """Normalise un montant pour la signature ET pour le corps JSON.

    Le meme rendu doit etre utilise des deux cotes, sinon la signature
    ne correspondra pas au payload envoye. On rend un entier sans
    decimale quand c'est possible (500 et non 500.00), sinon deux
    decimales.
    """
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    if value == value.to_integral_value():
        return str(int(value))
    return f"{value:.2f}"


def build_payload(
    *,
    amount: Decimal | int | float | str,
    method: str,
    recipient: str,
    reference: str,
    timestamp: int,
) -> str:
    return "|".join(
        [
            format_amount(amount),
            method,
            recipient,
            reference,
            str(timestamp),
        ]
    )


def sign(payload: str, client_secret: str) -> str:
    return hmac.new(
        client_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_withdrawal(
    *,
    amount: Decimal | int | float | str,
    method: str,
    recipient: str,
    reference: str,
    client_secret: str,
    timestamp: int | None = None,
) -> tuple[str, int, str]:
    """Retourne (signature, timestamp, montant_formate).

    Le montant formate doit etre reinjecte tel quel dans le corps JSON
    de l'etape 2 puis de l'etape 3.
    """
    ts = int(time.time()) if timestamp is None else int(timestamp)
    formatted = format_amount(amount)
    payload = build_payload(
        amount=formatted,
        method=method,
        recipient=recipient,
        reference=reference,
        timestamp=ts,
    )
    return sign(payload, client_secret), ts, formatted
