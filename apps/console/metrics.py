"""Chiffres de la console : lectures seules sur les modeles existants.

Rien ici n'ecrit, ne verrouille ni n'appelle un fournisseur : ces
fonctions tournent a chaque affichage de page.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from django.conf import settings
from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.transactions import services as txn_services
from apps.transactions.models import Transaction
from apps.transactions.states import State


def exceptions_to_handle(now=None) -> int:
    """Transferts qui attendent un humain : la pastille du menu.

    Plus etroit que l'ecran Exceptions, qui montre aussi les retraits en
    cours depuis peu : ceux-la sont normaux, une pastille ne doit pas
    s'allumer a chaque decaissement.
    """
    now = now or timezone.now()
    pending_before = now - timedelta(seconds=settings.PAYOUT_PENDING_STALE_SECONDS)
    in_flight_before = now - timedelta(seconds=settings.PAYOUT_INFLIGHT_STALE_SECONDS)
    return (
        Transaction.objects.with_state_since()
        .annotate(since=Coalesce("state_since", "updated_at"))
        .filter(
            Q(state__in=(State.PAYOUT_FAILED, State.PAYOUT_UNKNOWN))
            | Q(state=State.PAYMENT_CONFIRMED, failure_code__in=txn_services.HOLD_CODES)
            | Q(state=State.PAYOUT_PENDING, since__lt=pending_before)
            | Q(state=State.PAYOUT_IN_FLIGHT, since__lt=in_flight_before)
        )
        .count()
    )


def open_claims() -> int:
    from apps.claims.models import Claim, Status

    return Claim.objects.filter(status=Status.OPEN).count()


# ----------------------------------------------------------------------
# Tableau de bord
# ----------------------------------------------------------------------
#: Couleurs des routes dans le graphique : du plus fonce au plus clair,
#: la route la plus chargee recoit la plus foncee.
ROUTE_COLORS = ("#14532d", "#4d7c0f", "#84cc16", "#bef264", "#86efac", "#d9f99d")


def delivered(hours: int = 24, now=None) -> dict:
    """Transferts livres sur la periode : nombre, volume, commission."""
    from django.db.models import Count, Sum

    now = now or timezone.now()
    totals = Transaction.objects.filter(
        state=State.COMPLETED, payout_completed_at__gte=now - timedelta(hours=hours)
    ).aggregate(count=Count("id"), volume=Sum("net_amount"), fees=Sum("fee_platform"))
    return {
        "count": totals["count"],
        "volume": totals["volume"] or 0,
        "platform_fees": totals["fees"] or 0,
    }


def daily_volume(days: int = 14, now=None) -> dict:
    """Volume livre par jour (heure d'Haiti) et par route, pour les barres.

    Les hauteurs sont calculees ici, en pourcentage du jour le plus
    charge : le gabarit n'a qu'a les poser, sans bibliotheque de
    graphiques.
    """
    from django.db.models import Sum
    from django.db.models.functions import TruncDate

    from apps.transactions.models import Wallet

    now = timezone.localtime(now or timezone.now())
    first_day = now.date() - timedelta(days=days - 1)
    rows = (
        Transaction.objects.filter(
            state=State.COMPLETED,
            payout_completed_at__gte=timezone.make_aware(datetime.combine(first_day, time.min)),
        )
        .annotate(day=TruncDate("payout_completed_at", tzinfo=timezone.get_current_timezone()))
        .values("day", "source_wallet", "destination_wallet")
        .annotate(volume=Sum("net_amount"))
    )
    per_day: dict = {}
    per_route: dict = {}
    for row in rows:
        route = (row["source_wallet"], row["destination_wallet"])
        per_day.setdefault(row["day"], {})[route] = row["volume"]
        per_route[route] = per_route.get(route, 0) + row["volume"]

    ordered = sorted(per_route, key=lambda r: -per_route[r])
    colors = {route: ROUTE_COLORS[min(i, len(ROUTE_COLORS) - 1)] for i, route in enumerate(ordered)}
    totals = {day: sum(v.values()) for day, v in per_day.items()}
    peak = max(totals.values(), default=0)

    bars = []
    for offset in range(days):
        day = first_day + timedelta(days=offset)
        volumes = per_day.get(day, {})
        total = totals.get(day, 0)
        bars.append(
            {
                "day": day,
                "total": total,
                "height": round(float(total) / float(peak) * 100, 1) if peak else 0,
                # Du bas vers le haut : la route principale en bas.
                "segments": [
                    {
                        "color": colors[route],
                        "share": round(float(volumes[route]) / float(total) * 100, 1),
                    }
                    for route in reversed(ordered)
                    if volumes.get(route)
                ],
            }
        )
    return {
        "bars": bars,
        "peak": peak,
        "total": sum(per_route.values()),
        "routes": [
            {
                "label": f"{Wallet(src).label} → {Wallet(dst).label}",
                "color": colors[(src, dst)],
                "volume": per_route[(src, dst)],
            }
            for src, dst in ordered
        ],
    }


#: Transferts en cours, regroupes comme les voit l'exploitation.
IN_FLIGHT_GROUPS = (
    ("awaiting", "Attente du paiement", (State.AWAITING_PAYMENT,), "#cbd5e1"),
    ("queued", "En file", (State.PAYMENT_CONFIRMED, State.PAYOUT_QUEUED), "#a3e635"),
    ("sending", "Décaissement en cours", (State.PAYOUT_IN_FLIGHT,), "#4d7c0f"),
    ("check", "À vérifier", (State.PAYOUT_UNKNOWN, State.PAYOUT_PENDING, State.PAYOUT_FAILED), "#f59e0b"),
)


def in_flight() -> dict:
    from django.db.models import Count, Sum

    rows = {
        row["state"]: row
        for row in Transaction.objects.filter(
            state__in=[s for _, _, states, _ in IN_FLIGHT_GROUPS for s in states]
        )
        .values("state")
        .annotate(n=Count("id"), amount=Sum("net_amount"))
    }
    groups = []
    for key, label, states, color in IN_FLIGHT_GROUPS:
        count = sum(rows[s]["n"] for s in states if s in rows)
        amount = sum(rows[s]["amount"] for s in states if s in rows)
        groups.append({"key": key, "label": label, "count": count, "amount": amount, "color": color})
    total = sum(g["count"] for g in groups)
    for group in groups:
        group["share"] = round(group["count"] / total * 100, 1) if total else 0
    return {"groups": groups, "total": total}


def health(*, float_level: str, now=None) -> list[dict]:
    """Etat des maillons, lu en base : aucun appel a un fournisseur.

    status : « ok », « warn » ou « bad ».
    """
    from apps.notifications.models import Notification
    from apps.notifications.models import Status as NoteStatus
    from apps.transactions.models import TransactionEvent
    from apps.transactions.queue import payout_worker_status

    now = now or timezone.now()
    rows = []

    worker = payout_worker_status(now)
    if worker.stalled:
        rows.append({"label": "Worker de décaissement", "status": "bad", "detail": "à l'arrêt, file non vide"})
    elif worker.depth:
        rows.append({"label": "Worker de décaissement", "status": "ok", "detail": f"{worker.depth} en file"})
    else:
        rows.append({"label": "Worker de décaissement", "status": "ok", "detail": "file vide"})

    def last(state):
        return (
            TransactionEvent.objects.filter(to_state=state)
            .order_by("-created_at", "-id")
            .values_list("created_at", flat=True)
            .first()
        )

    success, failure = last(State.COMPLETED), last(State.PAYOUT_FAILED)
    rows.append(
        {
            "label": "Retraits plopplop",
            "status": "warn" if failure and (not success or failure > success) else "ok",
            "success_at": success,
            "failure_at": failure,
        }
    )

    failed_notes = Notification.objects.filter(
        status=NoteStatus.FAILED, updated_at__gte=now - timedelta(hours=24)
    ).count()
    rows.append(
        {
            "label": "Avis clients (SMS, WhatsApp)",
            "status": "warn" if failed_notes else "ok",
            "detail": f"{failed_notes} échec(s) sur 24 h" if failed_notes else "aucun échec sur 24 h",
        }
    )

    from apps.notifications import routing
    from apps.notifications.models import Provider

    otp_provider, notification_provider = routing.otp_provider(), routing.notification_provider()
    labels = dict(Provider.choices)
    otp_missing = routing.otp_missing(otp_provider)
    notes_missing = routing.notifications_missing(notification_provider)
    rows.append(
        {
            "label": "Messagerie",
            # Sans codes, aucun client ne se connecte : incident. Sans avis,
            # les transferts passent : a surveiller.
            "status": "bad" if otp_missing else ("warn" if notes_missing else "ok"),
            "detail": (
                f"codes : {labels[otp_provider]}{' (non configuré)' if otp_missing else ''}"
                f" · avis : {labels[notification_provider]}{' (non configuré)' if notes_missing else ''}"
            ),
        }
    )

    level = float_level
    rows.append(
        {
            "label": "Float",
            "status": {"ok": "ok", "warning": "warn"}.get(level, "bad"),
            "detail": {"ok": "au-dessus des seuils", "warning": "sous le seuil d'alerte"}.get(level, "critique"),
        }
    )
    return rows
