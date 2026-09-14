"""Vues de la console d'exploitation.

Squelette : les vues rendent les donnees, les actions arrivent a
l'etape suivante. Principe a tenir -- aucune vue ne modifie un objet
directement, elle appelle un service qui journalise et ecrit au grand
livre.
"""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, render

from apps.transactions.models import Transaction
from apps.transactions.states import LIABILITY_STATES, State
from apps.treasury.models import FloatAlert
from apps.treasury.services import evaluate_float


@login_required
def dashboard(request):
    liabilities = Transaction.objects.liabilities()
    context = {
        "float": evaluate_float(),
        "liability_count": liabilities.count(),
        "liability_total": liabilities.aggregate(t=Sum("net_amount"))["t"] or 0,
        "queue_depth": Transaction.objects.payable().count(),
        "alerts": FloatAlert.objects.filter(acknowledged_at__isnull=True)[:5],
        "by_state": Transaction.objects.values("state").annotate(n=Count("id")).order_by("-n"),
    }
    return render(request, "console/dashboard.html", context)


@login_required
def transaction_list(request):
    qs = Transaction.objects.all()
    state = request.GET.get("state")
    if state:
        qs = qs.filter(state=state)
    return render(
        request,
        "console/transactions.html",
        {"transactions": qs[:100], "states": State.choices, "current_state": state},
    )


@login_required
def transaction_detail(request, reference: str):
    txn = get_object_or_404(Transaction, reference=reference)
    return render(
        request,
        "console/transaction_detail.html",
        {
            "txn": txn,
            "events": txn.events.select_related("actor"),
            "entries": txn.journal_entries.prefetch_related("lines__account"),
        },
    )


@login_required
def payout_queue(request):
    """L'ecran que l'operateur garde ouvert : ce qui attend d'etre verse."""
    return render(
        request,
        "console/queue.html",
        {"queue": Transaction.objects.payable()[:100]},
    )


@login_required
def exceptions(request):
    stuck = Transaction.objects.filter(
        state__in=[State.PAYOUT_FAILED, State.PAYOUT_UNKNOWN, State.PAYOUT_PENDING]
    )
    return render(request, "console/exceptions.html", {"transactions": stuck[:100]})


@login_required
def treasury(request):
    return render(
        request,
        "console/treasury.html",
        {"float": evaluate_float(), "alerts": FloatAlert.objects.all()[:50]},
    )
