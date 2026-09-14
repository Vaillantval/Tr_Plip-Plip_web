from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.ledger.models import LedgerAccount
from apps.ledger.services import FLOAT
from apps.transactions.models import Transaction
from apps.transactions.states import LIABILITY_STATES, State

from .models import FloatAlert, FloatSnapshot

ZERO = Decimal("0")

#: Decaissements lances mais pas encore passes au grand livre. Le float
#: chez plopplop a peut-etre deja baisse ; ils passent avant la file.
ENGAGED_OUTSIDE_QUEUE = (State.PAYOUT_IN_FLIGHT, State.PAYOUT_UNKNOWN, State.PAYOUT_PENDING)


class AlertAlreadyAcknowledged(ValueError):
    pass


def ledger_float_balance() -> Decimal:
    try:
        return LedgerAccount.objects.get(code=FLOAT).balance()
    except LedgerAccount.DoesNotExist:
        return ZERO


def outstanding_liability() -> Decimal:
    """Total du sur les transactions encaissees mais pas encore versees.

    C'est le chiffre qui compte pour savoir si on peut honorer nos
    engagements : un float superieur au seuil ne sert a rien s'il est
    inferieur a cette somme.
    """
    agg = Transaction.objects.filter(state__in=list(LIABILITY_STATES)).aggregate(
        total=Sum("net_amount")
    )
    return agg["total"] or ZERO


def burn_rate(hours: int = 24) -> Decimal:
    """Consommation de float sur la periode ecoulee.

    Net verse plus frais reellement factures : c'est ce qui sort du float
    (cf. ledger.record_payout_executed). Sans les frais, la runway est
    surestimee.
    """
    since = timezone.now() - timedelta(hours=hours)
    agg = Transaction.objects.filter(payout_completed_at__gte=since).aggregate(
        net=Sum("net_amount"), fees=Sum("payout_fee_actual")
    )
    return (agg["net"] or ZERO) + (agg["fees"] or ZERO)


def _runway(balance: Decimal, burn_24h: Decimal) -> Decimal | None:
    if burn_24h <= ZERO:
        return None
    return (balance / burn_24h) * Decimal("24")


def runway_hours() -> Decimal | None:
    """Heures restantes avant rupture, au rythme des dernieres 24 h."""
    return _runway(ledger_float_balance(), burn_rate(24))


def _alert_level(balance: Decimal, liability: Decimal) -> tuple[str | None, str]:
    thresholds = settings.TREASURY["THRESHOLDS"]
    if balance < liability:
        return FloatAlert.Level.CRITICAL, (
            f"Float insuffisant : {balance} HTG disponibles pour "
            f"{liability} HTG d'engagements en cours"
        )
    if balance < thresholds["critical"]:
        return FloatAlert.Level.CRITICAL, f"Float critique : {balance} HTG"
    if balance < thresholds["warning"]:
        return FloatAlert.Level.WARNING, f"Float bas : {balance} HTG"
    return None, ""


def float_status() -> dict:
    """Etat du float, en lecture pure : aucune ecriture.

    A utiliser pour tout affichage. Deux seuils, deux logiques :
      - seuil absolu : le solde descend sous un plancher configure ;
      - couverture : le solde ne couvre plus les engagements en cours.
    Le second est le plus important et le plus souvent oublie.
    """
    balance = ledger_float_balance()
    liability = outstanding_liability()
    burn = burn_rate(24)
    level, message = _alert_level(balance, liability)
    return {
        "balance": balance,
        "liability": liability,
        "coverage_ok": balance >= liability,
        "coverage_ratio": (balance / liability) if liability > ZERO else None,
        "coverage_gap": max(liability - balance, ZERO),
        "level": level or "ok",
        "message": message,
        "burn_24h": burn,
        "runway_hours": _runway(balance, burn),
        "thresholds": settings.TREASURY["THRESHOLDS"],
    }


def evaluate_float() -> dict:
    """Evalue le float et cree une alerte si un seuil est franchi.

    ECRIT en base. Reserve a la tache check_float_level : aucune vue ne
    doit l'appeler, sinon un simple affichage cree des alertes. Pour
    afficher le float, utiliser float_status().
    """
    status = float_status()
    level = None if status["level"] == "ok" else status["level"]

    if level is not None:
        recent = FloatAlert.objects.filter(
            level=level,
            acknowledged_at__isnull=True,
            created_at__gte=timezone.now() - timedelta(hours=1),
        ).exists()
        if not recent:
            FloatAlert.objects.create(level=level, balance=status["balance"], message=status["message"])

    runway = status["runway_hours"]
    return {
        "balance": str(status["balance"]),
        "liability": str(status["liability"]),
        "coverage_ok": status["coverage_ok"],
        "level": status["level"],
        "burn_24h": str(status["burn_24h"]),
        "runway_hours": str(runway) if runway is not None else None,
    }


def queue_coverage() -> dict:
    """Jusqu'ou la file de decaissement peut aller avec le float actuel.

    Le float disponible pour la file est le solde du grand livre moins
    les decaissements deja lances mais pas encore comptabilises (en vol,
    indetermines, en attente) : ceux-la passent avant.

    Chaque decaissement consomme le net plus les frais operateur, estimes
    comme dans _settle_payout (fee_in + fee_out) tant que le montant reel
    n'est pas connu.

    `stall_rank` est le rang (1 = prochain decaisse) de la premiere
    transaction que le float ne couvre pas ; None si toute la file passe.
    """
    balance = ledger_float_balance()
    engaged = ZERO
    for net, fee_in, fee_out in Transaction.objects.filter(
        state__in=list(ENGAGED_OUTSIDE_QUEUE)
    ).values_list("net_amount", "fee_in", "fee_out"):
        engaged += net + fee_in + fee_out
    available = balance - engaged

    depth = 0
    total_net = total_debit = ZERO
    stall_rank = None
    for net, fee_in, fee_out in Transaction.objects.payable().values_list("net_amount", "fee_in", "fee_out"):
        depth += 1
        total_net += net
        total_debit += net + fee_in + fee_out
        if stall_rank is None and total_debit > available:
            stall_rank = depth

    return {
        "ledger_balance": balance,
        "engaged_outside_queue": engaged,
        "available": available,
        "depth": depth,
        "total_net": total_net,
        "total_debit": total_debit,
        "stall_rank": stall_rank,
        "covered_count": depth if stall_rank is None else stall_rank - 1,
        "shortfall": max(total_debit - available, ZERO),
    }


def acknowledge_alert(alert: FloatAlert, *, user) -> FloatAlert:
    """Acquitte une alerte de float.

    Le premier acquittement fait foi : une alerte deja acquittee est
    refusee plutot que reecrite.
    """
    updated = FloatAlert.objects.filter(pk=alert.pk, acknowledged_at__isnull=True).update(
        acknowledged_by=user, acknowledged_at=timezone.now()
    )
    if not updated:
        raise AlertAlreadyAcknowledged(f"Alerte {alert.pk} deja acquittee")
    alert.refresh_from_db()
    return alert


def record_snapshot(*, provider_balance: Decimal, transaction=None) -> FloatSnapshot:
    """A appeler apres chaque retrait reussi, avec le balance_after de plopplop."""
    return FloatSnapshot.objects.create(
        provider_balance=provider_balance,
        ledger_balance=ledger_float_balance(),
        transaction=transaction,
    )
