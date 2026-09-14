from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from apps.ledger.models import LedgerAccount
from apps.ledger.services import FLOAT
from apps.transactions.models import Transaction
from apps.transactions.states import LIABILITY_STATES

from .models import FloatAlert, FloatSnapshot

ZERO = Decimal("0")


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
    """Consommation de float sur la periode ecoulee."""
    since = timezone.now() - timedelta(hours=hours)
    agg = Transaction.objects.filter(payout_completed_at__gte=since).aggregate(
        total=Sum("net_amount")
    )
    return agg["total"] or ZERO


def runway_hours() -> Decimal | None:
    """Heures restantes avant rupture, au rythme des dernieres 24 h."""
    rate = burn_rate(24)
    if rate <= ZERO:
        return None
    return (ledger_float_balance() / rate) * Decimal("24")


def evaluate_float() -> dict:
    """Evalue le niveau de float et cree une alerte si un seuil est franchi.

    Deux seuils, deux logiques :
      - seuil absolu : le solde descend sous un plancher configure ;
      - couverture : le solde ne couvre plus les engagements en cours.
    Le second est le plus important et le plus souvent oublie.
    """
    balance = ledger_float_balance()
    liability = outstanding_liability()
    thresholds = settings.TREASURY["THRESHOLDS"]

    level = None
    message = ""

    if balance < liability:
        level = FloatAlert.Level.CRITICAL
        message = (
            f"Float insuffisant : {balance} HTG disponibles pour "
            f"{liability} HTG d'engagements en cours"
        )
    elif balance < thresholds["critical"]:
        level = FloatAlert.Level.CRITICAL
        message = f"Float critique : {balance} HTG"
    elif balance < thresholds["warning"]:
        level = FloatAlert.Level.WARNING
        message = f"Float bas : {balance} HTG"

    if level is not None:
        recent = FloatAlert.objects.filter(
            level=level,
            acknowledged_at__isnull=True,
            created_at__gte=timezone.now() - timedelta(hours=1),
        ).exists()
        if not recent:
            FloatAlert.objects.create(level=level, balance=balance, message=message)

    return {
        "balance": str(balance),
        "liability": str(liability),
        "coverage_ok": balance >= liability,
        "level": level or "ok",
        "burn_24h": str(burn_rate(24)),
        "runway_hours": str(runway_hours()) if runway_hours() is not None else None,
    }


def record_snapshot(*, provider_balance: Decimal, transaction=None) -> FloatSnapshot:
    """A appeler apres chaque retrait reussi, avec le balance_after de plopplop."""
    return FloatSnapshot.objects.create(
        provider_balance=provider_balance,
        ledger_balance=ledger_float_balance(),
        transaction=transaction,
    )
