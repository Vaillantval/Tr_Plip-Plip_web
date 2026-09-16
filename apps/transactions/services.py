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
from apps.treasury import services as treasury

from . import queue
from .models import PAYOUT_CAPABLE, PricingPolicy, Transaction, Wallet, WalletSetting
from .pricing import Quote, quote, round_htg
from .states import IllegalTransition, State

logger = logging.getLogger(__name__)

#: Le motif et la reference du transfert finissent tous deux dans
#: JournalEntry.description (255 car.), avec leurs libelles.
REFUND_REASON_MAX_LENGTH = 160
TRANSFER_REFERENCE_MAX_LENGTH = 64


class UnsupportedRoute(ValueError):
    pass


class MethodDisabled(UnsupportedRoute):
    """Moyen de paiement ferme par le superadmin."""


class PaymentStartFailed(Exception):
    """plopplop a refuse explicitement la creation du paiement."""


class InvalidRefund(ValueError):
    pass


class InvalidRelease(ValueError):
    pass


class InvalidPricing(ValueError):
    pass


#: Encaissement confirme mais bloque avant la file de decaissement.
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"  # plopplop declare un autre montant que le devis
AMOUNT_UNVERIFIED = "AMOUNT_UNVERIFIED"  # plopplop ne communique pas le montant
HOLD_CODES = (AMOUNT_MISMATCH, AMOUNT_UNVERIFIED)


def is_payment_held(txn: Transaction) -> bool:
    return txn.state == State.PAYMENT_CONFIRMED and txn.failure_code in HOLD_CODES


DIRECTIONS = ("payment", "payout")


# ----------------------------------------------------------------------
# Moyens de paiement
# ----------------------------------------------------------------------
RATE_FIELDS = ("payment_fee_rate", "payout_fee_rate", "payment_cost_rate", "payout_cost_rate")
#: Taux autorises : [0 ; 50 %[. Au-dela, c'est une erreur de saisie.
MAX_RATE = Decimal("0.5")
ZERO = Decimal("0")
#: Montant de reference pour l'apercu des marges par route.
PREVIEW_NET_AMOUNT = Decimal("1000")


def wallet_availability() -> list[dict]:
    """Etat de chaque portefeuille, dans l'ordre de Wallet. Lecture seule."""
    settings_by_wallet = {s.wallet: s for s in WalletSetting.objects.select_related("updated_by")}
    rows = []
    for wallet in Wallet:
        setting = settings_by_wallet.get(wallet.value)
        row = {
            "wallet": wallet.value,
            "label": wallet.label,
            "payout_capable": wallet in PAYOUT_CAPABLE,
            "payment_enabled": bool(setting and setting.payment_enabled),
            "payout_enabled": bool(setting and setting.payout_enabled),
            "updated_by": setting.updated_by if setting else None,
            "updated_at": setting.updated_at if setting else None,
        }
        for field in RATE_FIELDS:
            row[field] = getattr(setting, field) if setting else ZERO
        rows.append(row)
    return rows


def pricing_policy() -> PricingPolicy:
    policy, _ = PricingPolicy.objects.get_or_create(pk=1)
    return policy


def pricing_overview() -> dict:
    """Tarifs, couts et marge estimee de chaque route ouverte. Lecture seule.

    `loss_routes` : routes ouvertes dont la marge estimee est negative
    sur PREVIEW_NET_AMOUNT. `unreviewed` : aucun superadmin n'a encore
    enregistre la tarification.
    """
    policy = PricingPolicy.objects.select_related("reviewed_by").filter(pk=1).first() or PricingPolicy(pk=1)
    wallets = wallet_availability()
    by_code = {w["wallet"]: w for w in wallets}
    routes = []
    for source in wallets:
        for destination in wallets:
            if source is destination or not destination["payout_capable"]:
                continue
            q = quote(
                net_amount=PREVIEW_NET_AMOUNT,
                source_wallet=source["wallet"],
                destination_wallet=destination["wallet"],
                rates={
                    "in": source["payment_fee_rate"],
                    "out": destination["payout_fee_rate"],
                    "platform": policy.platform_fee_rate,
                },
            )
            cost_in, cost_out = _provider_costs(q, by_code[source["wallet"]], by_code[destination["wallet"]])
            margin = q.total_fees - cost_in - cost_out
            routes.append(
                {
                    "source": source,
                    "destination": destination,
                    "open": source["payment_enabled"] and destination["payout_enabled"],
                    "quote": q,
                    "cost_in": cost_in,
                    "cost_out": cost_out,
                    "margin": margin,
                }
            )
    return {
        "policy": policy,
        "wallets": wallets,
        "routes": routes,
        "preview_net_amount": PREVIEW_NET_AMOUNT,
        "unreviewed": policy.reviewed_at is None,
        "loss_routes": [r for r in routes if r["open"] and r["margin"] < 0],
    }


@db_transaction.atomic
def update_pricing(*, platform_fee_rate: Decimal, wallet_rates: dict[str, dict[str, Decimal]], actor) -> PricingPolicy:
    """Enregistre tarifs client et couts plopplop. Vaut validation.

    Ne touche aucune transaction existante : le devis et les couts
    estimes sont figes sur chaque transaction a sa creation.
    """
    _check_rate("Commission Plip-Plip", platform_fee_rate)
    for wallet, rates in wallet_rates.items():
        if wallet not in Wallet.values:
            raise InvalidPricing(f"Portefeuille inconnu : {wallet}")
        label = Wallet(wallet).label
        for field in RATE_FIELDS:
            _check_rate(f"{label} {field}", rates[field])
        if wallet not in [w.value for w in PAYOUT_CAPABLE] and (rates["payout_fee_rate"] or rates["payout_cost_rate"]):
            raise InvalidPricing(f"{label} ne recoit pas de decaissement : taux de sortie a 0")

    for wallet, rates in wallet_rates.items():
        setting, _ = WalletSetting.objects.select_for_update().get_or_create(wallet=wallet)
        for field in RATE_FIELDS:
            setattr(setting, field, rates[field])
        setting.updated_by = actor
        setting.save()

    policy, _ = PricingPolicy.objects.select_for_update().get_or_create(pk=1)
    policy.platform_fee_rate = platform_fee_rate
    policy.reviewed_by = actor
    policy.reviewed_at = timezone.now()
    policy.save()
    return policy


def _check_rate(label: str, rate) -> None:
    if rate is None or rate < ZERO or rate >= MAX_RATE:
        raise InvalidPricing(f"{label} : taux attendu entre 0 et {MAX_RATE * 100:.0f} %")


def _provider_costs(q: Quote, source, destination) -> tuple[Decimal, Decimal]:
    """Couts plopplop estimes : encaissement sur le montant paye, retrait sur le net."""
    return (
        round_htg(q.total_charged * source["payment_cost_rate"]),
        round_htg(q.net_amount * destination["payout_cost_rate"]),
    )


@db_transaction.atomic
def set_wallet_availability(wallet: str, *, direction: str, enabled: bool, actor=None) -> WalletSetting:
    """Ouvre ou ferme un portefeuille en entree ou en sortie.

    Ne touche aucune transaction existante : voir WalletSetting.
    """
    if wallet not in Wallet.values:
        raise UnsupportedRoute(f"Portefeuille inconnu : {wallet}")
    if direction not in DIRECTIONS:
        raise UnsupportedRoute(f"Sens inconnu : {direction}")
    if direction == "payout" and enabled and wallet not in [w.value for w in PAYOUT_CAPABLE]:
        raise UnsupportedRoute(f"{Wallet(wallet).label} ne peut pas recevoir de decaissement")

    setting, _ = WalletSetting.objects.select_for_update().get_or_create(wallet=wallet)
    setattr(setting, f"{direction}_enabled", enabled)
    setting.updated_by = actor
    setting.save()
    return setting


def _check_route(source_wallet: str, destination_wallet: str) -> tuple[WalletSetting, WalletSetting]:
    if destination_wallet not in [w.value for w in PAYOUT_CAPABLE]:
        raise UnsupportedRoute(
            f"Decaissement impossible vers {destination_wallet} : "
            "seuls MonCash et NatCash acceptent un retrait"
        )
    if source_wallet == destination_wallet:
        raise UnsupportedRoute("Les portefeuilles source et destination sont identiques")
    if source_wallet not in Wallet.values:
        raise UnsupportedRoute(f"Portefeuille source inconnu : {source_wallet}")

    enabled = {s.wallet: s for s in WalletSetting.objects.filter(wallet__in=[source_wallet, destination_wallet])}
    source, destination = enabled.get(source_wallet), enabled.get(destination_wallet)
    if source is None or not source.payment_enabled:
        raise MethodDisabled(f"{Wallet(source_wallet).label} est momentanement indisponible en envoi")
    if destination is None or not destination.payout_enabled:
        raise MethodDisabled(f"{Wallet(destination_wallet).label} est momentanement indisponible en reception")
    return source, destination


def _quote_route(*, source_wallet: str, destination_wallet: str, net_amount: Decimal):
    source, destination = _check_route(source_wallet, destination_wallet)
    q = quote(
        net_amount=net_amount,
        source_wallet=source_wallet,
        destination_wallet=destination_wallet,
        rates={
            "in": source.payment_fee_rate,
            "out": destination.payout_fee_rate,
            "platform": pricing_policy().platform_fee_rate,
        },
        max_net=settings.PRICING["MAX_NET_AMOUNT"],
    )
    return q, source, destination


def quote_transfer(*, source_wallet: str, destination_wallet: str, net_amount: Decimal) -> Quote:
    """Devis pour une route ouverte, aux tarifs regles par le superadmin.
    Leve UnsupportedRoute, MethodDisabled, AmountTooSmall ou AmountTooLarge.
    N'ecrit rien.
    """
    q, _, _ = _quote_route(source_wallet=source_wallet, destination_wallet=destination_wallet, net_amount=net_amount)
    return q


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
    customer=None,
) -> Transaction:
    q, source, destination = _quote_route(
        source_wallet=source_wallet,
        destination_wallet=destination_wallet,
        net_amount=net_amount,
    )
    cost_in, cost_out = _provider_costs(
        q,
        {"payment_cost_rate": source.payment_cost_rate},
        {"payout_cost_rate": destination.payout_cost_rate},
    )
    return Transaction.objects.create(
        provider_fee_in=cost_in,
        provider_fee_out_estimate=cost_out,
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
        customer=customer,
    )


def start_payment(txn: Transaction) -> Transaction:
    """Cree la jambe entrante chez plopplop et passe en attente paiement.

    A appeler UNE seule fois par transaction. Issue inconnue (timeout,
    5xx) : on passe quand meme en AWAITING_PAYMENT, sans jamais rappeler
    api/paiement-marchand pour cette reference. Un second appel pourrait
    declencher une seconde demande USSD sur le telephone du payeur. C'est
    sans risque de perte : api/paiement-verify interroge par REFERENCE,
    le polling detectera donc un paiement meme sans identifiant plopplop,
    et la transaction expirera sinon.

    Refus explicite de plopplop : la transaction est annulee et
    PaymentStartFailed est levee.
    """
    if txn.state != State.CREATED:
        raise ValueError(f"start_payment appele sur une transaction en etat {txn.state}")

    method = txn.source_wallet
    if method == Wallet.MONCASH and txn.sender_phone:
        method = "moncash_ussd"

    txn.payment_expires_at = timezone.now() + timedelta(seconds=settings.PAYMENT_EXPIRY_SECONDS)
    try:
        intent = get_client().create_payment(
            reference=txn.reference,
            amount=txn.total_charged,
            method=method,
            phone_number=txn.sender_phone or None,
        )
    except pp.PlopPlopIndeterminate as exc:
        logger.warning("Creation du paiement indeterminee sur %s : %s", txn.reference, exc)
        txn.save(update_fields=["payment_expires_at", "updated_at"])
        txn.transition(
            State.AWAITING_PAYMENT,
            note=f"Creation du paiement indeterminee ({method}) — suivi par reference, jamais recree",
            data={"error": str(exc), "indeterminate": True},
        )
        return txn
    except pp.PlopPlopError as exc:
        txn.failure_code = exc.code or "PAYMENT_CREATE_FAILED"
        txn.failure_message = str(exc)
        txn.save(update_fields=["failure_code", "failure_message", "updated_at"])
        txn.transition(State.CANCELLED, note=f"Paiement refuse par plopplop ({method})", data={"error": str(exc)})
        raise PaymentStartFailed(str(exc)) from exc

    txn.payment_provider_id = intent.transaction_id
    txn.payment_redirect_url = intent.redirect_url or ""
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
        if status.amount is None:
            # plopplop confirme sans dire combien : on ne verse rien sur
            # un montant non verifie.
            return _hold_payment(txn, received=None, code=AMOUNT_UNVERIFIED)
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

    Tout ecart entre `provider_amount` et le devis bloque la transaction
    avant la file, dans les deux sens : un paiement insuffisant ferait
    verser le net complet sur le float, un trop-percu laisserait un
    excedent du au payeur sans trace. Le montant absent de la reponse
    plopplop est traite dans poll_payment, seul appelant qui interroge
    l'operateur ; ici, provider_amount=None signifie que le montant a ete
    verifie par ailleurs.
    """
    if provider_amount is not None and provider_amount != txn.total_charged:
        return _hold_payment(txn, received=provider_amount, code=AMOUNT_MISMATCH)

    txn.transition(
        State.PAYMENT_CONFIRMED,
        note="Paiement confirme par plopplop",
        data={"provider_amount": str(provider_amount) if provider_amount else None},
    )
    if provider_amount is not None:
        txn.payment_amount_received = provider_amount
        txn.save(update_fields=["payment_amount_received", "updated_at"])
    ledger.record_payment_received(txn)
    txn.transition(State.PAYOUT_QUEUED, note="Mise en file de decaissement")
    queue.announce_on_queue_entry(txn)
    return txn


@db_transaction.atomic
def _hold_payment(txn: Transaction, *, received: Decimal | None, code: str) -> Transaction:
    """Encaissement non conforme : confirme, jamais mis en file.

    La transaction reste en PAYMENT_CONFIRMED et remonte sur l'ecran
    exceptions. Sortie par l'operateur uniquement : remboursement du
    montant recu, ou deblocage apres verification du montant exact.
    """
    if code == AMOUNT_MISMATCH:
        message = f"Montant recu {received} HTG different du devis {txn.total_charged} HTG"
    else:
        message = f"Montant encaisse non communique par plopplop (devis {txn.total_charged} HTG)"
    logger.error("Encaissement bloque sur %s : %s", txn.reference, message)

    txn.payment_amount_received = received
    txn.failure_code = code
    txn.failure_message = message
    txn.save(update_fields=["payment_amount_received", "failure_code", "failure_message", "updated_at"])
    txn.transition(
        State.PAYMENT_CONFIRMED,
        note=f"Paiement confirme mais bloque : {message}",
        data={"code": code, "expected": str(txn.total_charged), "received": str(received) if received is not None else None},
    )
    if received is not None:
        ledger.record_payment_held(txn, received=received)
    return txn


@db_transaction.atomic
def release_held_payment(txn: Transaction, *, verified_amount: Decimal, reason: str, actor=None) -> Transaction:
    """Debloque un encaissement apres verification manuelle chez plopplop.

    Uniquement si le montant verifie est EXACTEMENT celui du devis : un
    ecart reel ne se debloque pas, il se rembourse. L'ecriture de blocage
    est extournee et remplacee par l'ecriture d'encaissement normale.
    """
    reason = (reason or "").strip()
    if not is_payment_held(txn):
        raise InvalidRelease(f"{txn.reference} n'est pas un encaissement bloque")
    if not reason:
        raise InvalidRelease("Motif du deblocage obligatoire")
    if len(reason) > REFUND_REASON_MAX_LENGTH:
        raise InvalidRelease(f"Motif trop long (max {REFUND_REASON_MAX_LENGTH} caracteres)")
    if verified_amount != txn.total_charged:
        raise InvalidRelease(
            f"Montant verifie {verified_amount} HTG different du devis {txn.total_charged} HTG : "
            "seul un remboursement est possible"
        )

    hold_entry = txn.journal_entries.filter(reference=f"{txn.reference}-HOLD", reverses__isnull=True).first()
    if hold_entry is not None:
        ledger.reverse(hold_entry, reason=f"deblocage — {reason}", posted_by=actor)
    previous_code = txn.failure_code
    txn.payment_amount_received = verified_amount
    txn.failure_code = ""
    txn.failure_message = ""
    txn.save(update_fields=["payment_amount_received", "failure_code", "failure_message", "updated_at"])
    ledger.record_payment_received(txn)
    txn.transition(
        State.PAYOUT_QUEUED,
        actor=actor,
        note=f"Encaissement debloque apres verification ({verified_amount} HTG) — {reason}",
        data={"released_from": previous_code, "verified_amount": str(verified_amount), "reason": reason},
    )
    queue.announce_on_queue_entry(txn)
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
        return _settle_payout(
            txn,
            result_fee=result.fee,
            provider_id=result.transaction_id,
            api_reference=result.api_reference or "",
            balance_after=result.balance_after,
        )

    return _fail_payout(txn, code="API_TRANSFER_FAILED", message=result.raw.get("message", "Echec"))


def verify_unknown_payout(txn: Transaction, *, actor=None) -> Transaction:
    """Resout un PAYOUT_UNKNOWN ou un PAYOUT_PENDING en interrogeant l'operateur.

    Seule sortie autorisee de ces deux etats. Tant que le statut distant
    est 'pending', la transaction reste (ou passe) en PAYOUT_PENDING et
    sera revue plus tard. `actor` est l'utilisateur de la console qui a
    declenche la verification ; None pour la tache planifiee.
    """
    if txn.state not in (State.PAYOUT_UNKNOWN, State.PAYOUT_PENDING):
        return txn

    client = get_client()
    try:
        auth = client.authenticate()
        status = client.withdrawal_status(auth_token=auth, reference=txn.payout_reference)
    except pp.PlopPlopError as exc:
        if exc.status == 404 and txn.state == State.PAYOUT_PENDING:
            # VOLONTAIRE, PAS UN OUBLI : un 404 sur un PAYOUT_PENDING ne fait
            # aucune transition et ne remet JAMAIS en file.
            #
            # PENDING signifie que plopplop a deja repondu « ce retrait
            # existe, il est en cours ». Un 404 ensuite contredit cette
            # reponse (incident, purge, incoherence cote operateur) mais ne
            # prouve pas que l'argent n'est pas parti. Remettre en file,
            # c'est emettre un second retrait sous une nouvelle reference
            # (-W2, -W3...) vers un beneficiaire peut-etre deja paye : un
            # double paiement que rien ne rattraperait.
            #
            # La transaction reste en PENDING, remonte comme enlisee sur
            # l'ecran exceptions, et se tranche a la main avec l'operateur.
            logger.error(
                "Retrait %s introuvable alors qu'il etait en attente cote operateur",
                txn.payout_reference,
            )
            return txn
        if exc.status == 404:
            return _unknown_payout_not_found(txn, actor=actor)
        logger.warning("Verification impossible sur %s : %s", txn.reference, exc)
        return txn

    if status.status == "success":
        return _settle_payout(
            txn, result_fee=None, provider_id=status.transaction_id or "", api_reference="", actor=actor
        )
    if status.status == "failed":
        return _fail_payout(
            txn, code="API_TRANSFER_FAILED", message="Echec confirme par verification", actor=actor
        )
    if status.status in ("rembourse", "remboursé"):
        txn.transition(State.PAYOUT_FAILED, actor=actor, note="Retrait rembourse par l'operateur")
        return txn

    if txn.state == State.PAYOUT_PENDING:
        # Deja en attente, et toujours en attente : PENDING -> PENDING
        # n'existe pas dans la machine a etats. Rien a ecrire.
        return txn
    txn.transition(State.PAYOUT_PENDING, actor=actor, note="Retrait toujours en attente cote operateur")
    return txn


#: Marqueur, dans TransactionEvent.data, d'un 404 constate sur un PAYOUT_UNKNOWN.
VERIFY_NOT_FOUND = "verify_not_found"


def _unknown_payout_not_found(txn: Transaction, *, actor=None) -> Transaction:
    """404 sur un PAYOUT_UNKNOWN : remise en file au second constat seulement.

    Juste apres un timeout, plopplop peut ne pas encore exposer en lecture
    un retrait qu'il vient d'ecrire. Un 404 isole ne prouve donc pas que
    rien n'est parti. On exige deux 404 pour la meme reference de retrait,
    le premier et celui-ci espaces d'au moins PAYOUT_VERIFY_GRACE_SECONDS,
    sans sortie d'UNKNOWN entre les deux.

    Les constats sont lus dans le journal d'evenements (pas de champ
    dedie) : chaque 404 non concluant y laisse un evenement sans
    changement d'etat.
    """
    entered = (
        txn.events.filter(to_state=State.PAYOUT_UNKNOWN)
        .exclude(from_state=State.PAYOUT_UNKNOWN)
        .order_by("-created_at", "-id")
        .first()
    )
    first_not_found = None
    if entered is not None:
        first_not_found = (
            txn.events.filter(
                id__gt=entered.id,
                from_state=State.PAYOUT_UNKNOWN,
                to_state=State.PAYOUT_UNKNOWN,
                data__check=VERIFY_NOT_FOUND,
                data__payout_reference=txn.payout_reference,
            )
            .order_by("created_at", "id")
            .first()
        )

    grace = timedelta(seconds=settings.PAYOUT_VERIFY_GRACE_SECONDS)
    if first_not_found is None or timezone.now() - first_not_found.created_at < grace:
        txn.record_observation(
            actor=actor,
            note=(
                "Aucun retrait trouve — constat non concluant, "
                f"confirmation requise apres {settings.PAYOUT_VERIFY_GRACE_SECONDS} s"
            ),
            data={"check": VERIFY_NOT_FOUND, "payout_reference": txn.payout_reference},
        )
        return txn

    txn.transition(
        State.PAYOUT_FAILED,
        actor=actor,
        note="Aucun retrait trouve a deux verifications espacees — le decaissement n'est jamais parti",
        data={"first_not_found_at": first_not_found.created_at.isoformat()},
    )
    txn.transition(State.PAYOUT_QUEUED, actor=actor, note="Remise en file (nouvelle reference)")
    return txn


def retry_failed_payout(txn: Transaction, *, actor=None) -> Transaction:
    """Relance manuelle d'un decaissement echoue, depuis la console.

    Uniquement depuis PAYOUT_FAILED. Un PAYOUT_UNKNOWN n'arrive en
    PAYOUT_FAILED qu'a travers verify_unknown_payout : cette relance ne
    peut donc pas rejouer un retrait dont l'issue est inconnue. La garde
    explicite est necessaire, PAYMENT_CONFIRMED -> PAYOUT_QUEUED etant
    aussi une transition autorisee.
    """
    if txn.state != State.PAYOUT_FAILED:
        raise IllegalTransition(txn.state, State.PAYOUT_QUEUED)
    txn.transition(
        State.PAYOUT_QUEUED,
        actor=actor,
        note=f"Relance manuelle (tentative {txn.payout_attempts + 1} a venir)",
    )
    return txn


@db_transaction.atomic
def _settle_payout(
    txn,
    *,
    result_fee: Decimal | None,
    provider_id: str,
    api_reference: str,
    balance_after: Decimal | None = None,
    actor=None,
) -> Transaction:
    # Sans champ `fee` (issue tranchee par verification), on retient le cout
    # de retrait estime et fige a la creation.
    actual_fee = result_fee if result_fee is not None else txn.provider_fee_out_estimate
    txn.payout_provider_id = provider_id
    txn.payout_api_reference = api_reference
    txn.payout_fee_actual = actual_fee
    txn.save(
        update_fields=["payout_provider_id", "payout_api_reference", "payout_fee_actual", "updated_at"]
    )
    txn.transition(State.COMPLETED, actor=actor, note="Decaissement confirme", data={"fee": str(actual_fee)})
    ledger.record_payout_executed(txn, actual_fee=actual_fee)

    # balance_after n'est connu qu'au retour du retrait lui-meme, pas
    # d'une verification : pas de snapshot dans ce cas.
    if balance_after is not None:
        treasury.record_snapshot(provider_balance=balance_after, transaction=txn)

    if txn.margin_estimate < 0:
        logger.error("Marge negative sur %s : %s HTG", txn.reference, txn.margin_estimate)
    return txn


def _fail_payout(txn, *, code: str, message: str, actor=None) -> Transaction:
    txn.failure_code = code
    txn.failure_message = message
    txn.save(update_fields=["failure_code", "failure_message", "updated_at"])
    txn.transition(State.PAYOUT_FAILED, actor=actor, note=message, data={"code": code})

    if code in ("INSUFFICIENT_BALANCE", "METHOD_NOT_CONFIGURED"):
        # Causes systemiques : inutile de rejouer, un operateur doit agir.
        logger.critical("Decaissement bloque (%s) sur %s", code, txn.reference)
    elif txn.payout_attempts < settings.PAYOUT_MAX_ATTEMPTS:
        txn.transition(State.PAYOUT_QUEUED, actor=actor, note=f"Nouvelle tentative ({txn.payout_attempts + 1})")
    return txn


@db_transaction.atomic
def refund(
    txn: Transaction,
    *,
    reason: str,
    transfer_reference: str,
    refunded_amount: Decimal | None = None,
    actor=None,
) -> Transaction:
    """Constate un remboursement deja effectue, depuis la console.

    Aucun argent ne part d'ici : plopplop n'expose pas de remboursement.
    Le transfert vers le payeur a ete fait a la main, et sa reference est
    exigee pour imposer l'ordre : envoyer d'abord, enregistrer ensuite.

    Encaissement bloque : on rend ce qui a ete RECU, pas le devis.
      - montant declare par plopplop : `refunded_amount` doit lui etre egal
        (ou etre omis) ;
      - montant non communique : `refunded_amount` est obligatoire, c'est
        le montant verifie chez plopplop et rendu au payeur.
    """
    reason = (reason or "").strip()
    transfer_reference = (transfer_reference or "").strip()
    if not reason:
        raise InvalidRefund("Motif du remboursement obligatoire")
    if not transfer_reference:
        raise InvalidRefund("Reference du transfert manuel obligatoire")
    if len(reason) > REFUND_REASON_MAX_LENGTH:
        raise InvalidRefund(f"Motif trop long (max {REFUND_REASON_MAX_LENGTH} caracteres)")
    if len(transfer_reference) > TRANSFER_REFERENCE_MAX_LENGTH:
        raise InvalidRefund(f"Reference trop longue (max {TRANSFER_REFERENCE_MAX_LENGTH} caracteres)")

    if not is_payment_held(txn):
        txn.transition(
            State.REFUNDED,
            actor=actor,
            note=f"{reason} — transfert {transfer_reference}",
            data={"reason": reason, "transfer_reference": transfer_reference},
        )
        ledger.record_refund(txn, reason=reason, transfer_reference=transfer_reference, posted_by=actor)
        return txn

    received = txn.payment_amount_received
    if received is None:
        if refunded_amount is None or refunded_amount <= 0:
            raise InvalidRefund("Montant encaisse non communique : saisir le montant verifie et rembourse")
        amount = refunded_amount
    else:
        if refunded_amount is not None and refunded_amount != received:
            raise InvalidRefund(f"Montant rembourse {refunded_amount} HTG different du montant recu {received} HTG")
        amount = received

    txn.transition(
        State.REFUNDED,
        actor=actor,
        note=f"{reason} — transfert {transfer_reference} — {amount} HTG rendus",
        data={"reason": reason, "transfer_reference": transfer_reference, "refunded_amount": str(amount)},
    )
    if received is None:
        ledger.record_payment_held(txn, received=amount, posted_by=actor, memo="montant constate par l'operateur")
    ledger.record_held_refund(
        txn, amount=amount, reason=reason, transfer_reference=transfer_reference, posted_by=actor
    )
    return txn
