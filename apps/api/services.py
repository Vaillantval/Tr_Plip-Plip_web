"""Creation idempotente d'un transfert.

Aucune regle metier ici : le devis, la route et la machine a etats
restent dans apps.transactions. Ce module ne gere que ce qui est propre
a l'API -- la cle d'idempotence et le montant confirme par le client.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from django.db import IntegrityError, transaction as db_transaction

from apps.transactions import services as transactions
from apps.transactions.models import Transaction
from apps.transactions.pricing import Quote

from .models import IdempotencyKey


class IdempotencyKeyReused(Exception):
    """Meme cle, corps different."""


class IdempotencyInProgress(Exception):
    """Une requete concurrente utilise la meme cle."""


class QuoteChanged(Exception):
    def __init__(self, quote: Quote):
        super().__init__("Le devis a change depuis sa presentation au client")
        self.quote = quote


@dataclass(frozen=True)
class TransferOutcome:
    transaction: Transaction
    replayed: bool
    payment_failed: bool = False


def fingerprint(payload: dict) -> str:
    canonical = json.dumps({k: str(v) for k, v in sorted(payload.items())}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def create_transfer(
    *,
    customer,
    idempotency_key: str,
    source_wallet: str,
    destination_wallet: str,
    recipient_phone: str,
    net_amount: Decimal,
    expected_total: Decimal,
    sender_phone: str = "",
) -> TransferOutcome:
    payload = {
        "source_wallet": source_wallet,
        "destination_wallet": destination_wallet,
        "recipient_phone": recipient_phone,
        "net_amount": net_amount,
        "expected_total": expected_total,
        "sender_phone": sender_phone,
    }
    digest = fingerprint(payload)

    existing = _existing(customer, idempotency_key, digest)
    if existing is not None:
        return TransferOutcome(transaction=existing, replayed=True)

    quote = transactions.quote_transfer(
        source_wallet=source_wallet, destination_wallet=destination_wallet, net_amount=net_amount
    )
    if quote.total_charged != expected_total:
        raise QuoteChanged(quote)

    try:
        with db_transaction.atomic():
            txn = transactions.create_transaction(
                source_wallet=source_wallet,
                destination_wallet=destination_wallet,
                recipient_phone=recipient_phone,
                sender_phone=sender_phone,
                net_amount=net_amount,
                customer=customer,
            )
            IdempotencyKey.objects.create(
                customer=customer, key=idempotency_key, request_fingerprint=digest, transaction=txn
            )
    except IntegrityError:
        # Requete concurrente avec la meme cle, validee entre-temps.
        existing = _existing(customer, idempotency_key, digest)
        if existing is None:
            raise IdempotencyInProgress("Requete deja en cours avec cette cle")
        return TransferOutcome(transaction=existing, replayed=True)

    # La cle est enregistree AVANT l'appel a plopplop : un rejeu pendant ou
    # apres cet appel rend cette transaction et ne recree jamais de paiement.
    try:
        transactions.start_payment(txn)
    except transactions.PaymentStartFailed:
        txn.refresh_from_db()
        return TransferOutcome(transaction=txn, replayed=False, payment_failed=True)
    txn.refresh_from_db()
    return TransferOutcome(transaction=txn, replayed=False)


def _existing(customer, key: str, digest: str) -> Transaction | None:
    record = IdempotencyKey.objects.select_related("transaction").filter(customer=customer, key=key).first()
    if record is None:
        return None
    if record.request_fingerprint != digest:
        raise IdempotencyKeyReused("Cle Idempotency-Key deja utilisee pour une autre demande")
    return record.transaction
