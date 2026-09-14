"""Orchestration du moteur transactionnel.

Chaque fonction ici est le seul chemin autorise pour un evenement metier.
Les vues et les taches Celery appellent ces fonctions ; elles ne touchent
ni aux modeles ni au client plopplop directement.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from apps.ledger import services as ledger
from apps.providers.plopplop import exceptions as pp
from apps.providers.plopplop.client import get_client

from .models import PAYOUT_CAPABLE, Transaction, Wallet
from .pricing import quote_from_settings
from .states import State

logger = logging.getLogger(__name__)


class UnsupportedRoute(ValueError):
    pass


# ----------------------------------------------------------------------
# Creation
# ----------------------------------------------------------------------
@db_transaction.atomic
def create_transaction(
    *,
    source_wallet: str,
    destination_wallet: str,
    recipient_phone: str,
    net_amount: Decimal,
    sender_phone: str = "",
    created_by=None,
) -> Transaction:
    if destination_wallet not in [w.value for w in PAYOUT_CAPABLE]:
        raise UnsupportedRoute(
            f"Decaissement impossible vers {destination_wallet} : "
            "seuls MonCash et NatCash acceptent un retrait"
        )
    if source_wallet == destination_wallet:
        raise UnsupportedRoute("Les portefeuilles source et destination sont identiques")

    q = quote_from_settings(
        net_amount=net_amount,
        source_wallet=source_wallet,
        destination_wallet=destination_wallet,
    )
    return Transaction.objects.create(
        source_wallet=source_wallet,
        destination_wallet=destination_wallet,
        sender_phone=sender_phone,
        recipient_phone=recipient_phone,
        net_amount=q.net_amount,
        fee_in=q.fee_in,
        fee_out=q.fee_out,
        fee_platform=q.fee_platform,
        total_charged=q.total_charged,
        created_by=created_by,
    )


def start_payment(txn: Transaction) -> Transaction:
    """Cree la jambe entrante chez plopplop et passe en attente paiement."""
    if txn.state != State.CREATED:
        raise ValueError(f"start_payment appele sur une transaction en etat {txn.state}")

    method = txn.source_wallet
    if method == Wallet.MONCASH and txn.sender_phone:
        method = "moncash_ussd"

    intent = get_client().create_payment(
        reference=txn.reference,
        amount=txn.total_charged,
        method=method,
        phone_number=txn.sender_phone or None,
    )

    txn.payment_provider_id = intent.transaction_id
    txn.payment_redirect_url = intent.redirect_url or ""
    txn.payment_expires_at = timezone.now() + timedelta(seconds=settings.PAYMENT_EXPIRY_SECONDS)
    txn.save(
        update_fields=[
            "payment_provider_id",
            "payment_redirect_url",
            "payment_expires_at",
            "updated_at",
        ]
    )
    txn.transition(
        State.AWAITING_PAYMENT,
        note=f"Paiement cree ({method})",
        data={"provider_id": intent.transaction_id, "ussd": intent.is_ussd},
    )
    return txn


# ----------------------------------------------------------------------
# Encaissement
# ----------------------------------------------------------------------
def poll_payment(txn: Transaction) -> Transaction:
    """Interroge api/paiement-verify.

    L'API ne connait que 'no' et 'ok' : il n'existe pas d'etat d'echec.
    L'expiration est donc decidee ici, localement.
    """
    if txn.state != State.AWAITING_PAYMENT:
        return txn

    status = get_client().payment_status(txn.reference)
    txn.last_polled_at = timezone.now()
    txn.poll_count += 1
    txn.save(update_fields=["last_polled_at", "poll_count", "updated_at"])

    if status.confirmed:
        return confirm_payment(txn, provider_amount=status.amount)

    if txn.payment_expires_at and timezone.now() > txn.payment_expires_at:
        txn.transition(
            State.PAYMENT_EXPIRED,
            note=f"Aucune confirmation apres {txn.poll_count} verifications",
        )
    return txn


@db_transaction.atomic
def confirm_payment(txn: Transaction, *, provider_amount: Decimal | None = None) -> Transaction:
    """Encaissement confirme : ecriture comptable puis mise en file.

    A partir d'ici nous detenons l'argent du client. Tout etat entre ce
    point et COMPLETED est une dette.
    """
    if provider_amount is not None and provider_amount != txn.total_charged:
        # On n'interrompt pas -- l'argent est deja encaisse -- mais
        # l'ecart doit remonter en exception pour traitement manuel.
        logger.error(
            "Ecart de montant sur %s : attendu %s, recu %s",
            txn.reference,
            txn.total_charged,
            provider_amount,
        )

    txn.transition(
        State.PAYMENT_CONFIRMED,
        note="Paiement confirme par plopplop",
        data={"provider_amount": str(provider_amount) if provider_amount else None},
    )
    ledger.record_payment_received(txn)
    txn.transition(State.PAYOUT_QUEUED, note="Mise en file de decaissement")
    return txn


# ----------------------------------------------------------------------
# Decaissement
# ----------------------------------------------------------------------
def execute_payout(txn: Transaction) -> Transaction:
    """Execute le decaissement. A n'appeler QUE depuis le worker serialise.

    Le cooldown de 120 s par IP impose un seul retrait a la fois pour
    toute la plateforme ; la serialisation est assuree par le verrou
    dans tasks.py, pas ici.
    """
    if txn.state != State.PAYOUT_QUEUED:
        raise ValueError(f"execute_payout appele sur une transaction en etat {txn.state}")

    payout_ref = txn.build_payout_reference()
    txn.payout_reference = payout_ref
    txn.payout_attempts += 1
    txn.save(update_fields=["payout_reference", "payout_attempts", "updated_at"])
    txn.transition(
        State.PAYOUT_IN_FLIGHT,
        note=f"Tentative {txn.payout_attempts}",
        data={"payout_reference": payout_ref},
    )

    client = get_client()
    try:
        result = client.withdraw(
            amount=txn.net_amount,
            method=txn.destination_wallet,
            recipient=txn.recipient_phone,
            reference=payout_ref,
        )
    except pp.PlopPlopIndeterminate as exc:
        # Etat inconnu : l'argent est peut-etre parti. On ne rejoue pas.
        logger.warning("Decaissement indetermine sur %s : %s", txn.reference, exc)
        txn.transition(
            State.PAYOUT_UNKNOWN,
            note="Issue inconnue — verification requise",
            data={"error": str(exc)},
        )
        return txn
    except pp.DuplicateReference as exc:
        # Un retrait porte deja cette reference : ne rien conclure.
        logger.warning("Reference dupliquee sur %s : %s", txn.reference, exc)
        txn.transition(
            State.PAYOUT_UNKNOWN,
            note="Reference deja utilisee — verification requise",
            data={"error": str(exc)},
        )
        return txn
    except pp.WithdrawalCooldown as exc:
        # Le verrou a laisse passer un appel trop tot. On remet en file.
        logger.warning("Cooldown atteint sur %s : %s", txn.reference, exc)
        txn.transition(State.PAYOUT_FAILED, note="Cooldown operateur", data={"error": str(exc)})
        txn.transition(State.PAYOUT_QUEUED, note="Remise en file apres cooldown")
        return txn
    except pp.PlopPlopError as exc:
        return _fail_payout(txn, code=exc.code or "UNKNOWN", message=str(exc))

    if result.succeeded:
        return _settle_payout(txn, result_fee=result.fee, provider_id=result.transaction_id,
                              api_reference=result.api_reference or "")

    return _fail_payout(txn, code="API_TRANSFER_FAILED", message=result.raw.get("message", "Echec"))


def verify_unknown_payout(txn: Transaction) -> Transaction:
    """Resout un PAYOUT_UNKNOWN en interrogeant l'operateur.

    Seule sortie autorisee de cet etat. Tant que le statut distant est
    'pending', la transaction reste en attente et sera revue plus tard.
    """
    if txn.state != State.PAYOUT_UNKNOWN:
        return txn

    client = get_client()
    try:
        auth = client.authenticate()
        status = client.withdrawal_status(auth_token=auth, reference=txn.payout_reference)
    except pp.PlopPlopError as exc:
        # 404 : aucun retrait ne porte cette reference, rien n'est parti.
        if exc.status == 404:
            txn.transition(
                State.PAYOUT_FAILED,
                note="Aucun retrait trouve — le decaissement n'est jamais parti",
            )
            txn.transition(State.PAYOUT_QUEUED, note="Remise en file (nouvelle reference)")
            return txn
        logger.warning("Verification impossible sur %s : %s", txn.reference, exc)
        return txn

    if status.status == "success":
        return _settle_payout(txn, result_fee=None, provider_id=status.transaction_id or "", api_reference="")
    if status.status == "failed":
        return _fail_payout(txn, code="API_TRANSFER_FAILED", message="Echec confirme par verification")
    if status.status in ("rembourse", "remboursé"):
        txn.transition(State.PAYOUT_FAILED, note="Retrait rembourse par l'operateur")
        return txn

    txn.transition(State.PAYOUT_PENDING, note="Retrait toujours en attente cote operateur")
    return txn


@db_transaction.atomic
def _settle_payout(txn, *, result_fee: Decimal | None, provider_id: str, api_reference: str) -> Transaction:
    actual_fee = result_fee if result_fee is not None else (txn.fee_in + txn.fee_out)
    txn.payout_provider_id = provider_id
    txn.payout_api_reference = api_reference
    txn.payout_fee_actual = actual_fee
    txn.save(
        update_fields=["payout_provider_id", "payout_api_reference", "payout_fee_actual", "updated_at"]
    )
    txn.transition(State.COMPLETED, note="Decaissement confirme", data={"fee": str(actual_fee)})
    ledger.record_payout_executed(txn, actual_fee=actual_fee)

    if txn.margin_estimate < 0:
        logger.error("Marge negative sur %s : %s HTG", txn.reference, txn.margin_estimate)
    return txn


def _fail_payout(txn, *, code: str, message: str) -> Transaction:
    txn.failure_code = code
    txn.failure_message = message
    txn.save(update_fields=["failure_code", "failure_message", "updated_at"])
    txn.transition(State.PAYOUT_FAILED, note=message, data={"code": code})

    if code in ("INSUFFICIENT_BALANCE", "METHOD_NOT_CONFIGURED"):
        # Causes systemiques : inutile de rejouer, un operateur doit agir.
        logger.critical("Decaissement bloque (%s) sur %s", code, txn.reference)
    elif txn.payout_attempts < settings.PAYOUT_MAX_ATTEMPTS:
        txn.transition(State.PAYOUT_QUEUED, note=f"Nouvelle tentative ({txn.payout_attempts + 1})")
    return txn


@db_transaction.atomic
def refund(txn: Transaction, *, reason: str, actor=None) -> Transaction:
    """Remboursement manuel, declenche depuis la console."""
    txn.transition(State.REFUNDED, actor=actor, note=reason)
    ledger.record_refund(txn, reason=reason, posted_by=actor)
    return txn
