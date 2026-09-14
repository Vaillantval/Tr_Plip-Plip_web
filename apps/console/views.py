"""Vues de la console d'exploitation.

Principe tenu : aucune vue ne modifie un objet directement. Les lectures
passent par l'ORM ; toute ecriture passe par le service de l'app
proprietaire du modele, qui journalise et ecrit au grand livre.

Les actions repondent par le fragment HTMX de la zone qui les a
declenchees, ou par une redirection hors HTMX.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.ledger import services as ledger
from apps.transactions import services as txn_services
from apps.transactions.models import Transaction
from apps.transactions.states import IllegalTransition, State, can
from apps.treasury import services as treasury
from apps.treasury.models import FloatAlert, FloatSnapshot

from .forms import RefundForm, ReleaseForm, TopupForm
from .permissions import audit_failure, require_acting_role, require_superadmin

EXCEPTION_STATES = (State.PAYOUT_FAILED, State.PAYOUT_UNKNOWN, State.PAYOUT_PENDING)
LIST_LIMIT = 200


def _is_htmx(request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _seconds_since(moment, now) -> int | None:
    return int((now - moment).total_seconds()) if moment else None


def _form_errors(form) -> str:
    return " ; ".join(str(msg) for errors in form.errors.values() for msg in errors)


def available_actions(txn: Transaction) -> dict:
    """Actions proposees d'apres l'etat. Ne dit rien du role : le template
    le verifie pour l'affichage, chaque vue d'action le verifie a nouveau.
    """
    return {
        "retry": txn.state == State.PAYOUT_FAILED,
        "verify": txn.state in (State.PAYOUT_UNKNOWN, State.PAYOUT_PENDING),
        "refund": can(txn.state, State.REFUNDED),
        "release": txn_services.is_payment_held(txn),
    }


# ----------------------------------------------------------------------
# Ecrans
# ----------------------------------------------------------------------
@login_required
def dashboard(request):
    liabilities = Transaction.objects.liabilities()
    context = {
        "float": treasury.float_status(),
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


def _transaction_context(txn: Transaction, *, refund_form=None, release_form=None) -> dict:
    entries = []
    for entry in (
        txn.journal_entries.select_related("posted_by", "reverses")
        .prefetch_related("lines__account")
        .order_by("created_at", "id")
    ):
        lines = list(entry.lines.all())
        entries.append({"entry": entry, "lines": lines, "total": sum((l.amount for l in lines), Decimal("0"))})
    return {
        "txn": txn,
        "events": txn.events.select_related("actor"),
        "entries": entries,
        "actions": available_actions(txn),
        "refund_form": refund_form or RefundForm(),
        "release_form": release_form or ReleaseForm(),
        "max_attempts": settings.PAYOUT_MAX_ATTEMPTS,
    }


@login_required
def transaction_detail(request, reference: str):
    txn = get_object_or_404(Transaction, reference=reference)
    return render(request, "console/transaction_detail.html", _transaction_context(txn))


def _queue_context() -> dict:
    now = timezone.now()
    cooldown = settings.PAYOUT_COOLDOWN_SECONDS
    coverage = treasury.queue_coverage()
    stall_rank = coverage["stall_rank"]
    rows = [
        {
            "rank": rank,
            "txn": txn,
            "wait_seconds": _seconds_since(txn.payment_confirmed_at, now),
            "eta_seconds": rank * cooldown,
            "covered": stall_rank is None or rank < stall_rank,
            "stalls_here": rank == stall_rank,
        }
        for rank, txn in enumerate(Transaction.objects.payable()[:LIST_LIMIT], start=1)
    ]
    return {
        "rows": rows,
        "coverage": coverage,
        "depth": coverage["depth"],
        "cooldown": cooldown,
        "drain_seconds": coverage["depth"] * cooldown,
        "truncated": coverage["depth"] > len(rows),
        "now": now,
    }


@login_required
def payout_queue(request):
    """L'ecran que l'operateur garde ouvert : ce qui attend d'etre verse."""
    template = "console/partials/queue_body.html" if _is_htmx(request) else "console/queue.html"
    return render(request, template, _queue_context())


def _exceptions_context() -> dict:
    now = timezone.now()
    stale_after = settings.PAYOUT_PENDING_STALE_SECONDS
    grouped = {state: [] for state in EXCEPTION_STATES}
    held = []
    stale = []
    stuck = (
        Transaction.objects.filter(
            Q(state__in=EXCEPTION_STATES)
            | Q(state=State.PAYMENT_CONFIRMED, failure_code__in=txn_services.HOLD_CODES)
        )
        .with_state_since()
        .order_by("state_since", "id")[: LIST_LIMIT * (len(EXCEPTION_STATES) + 1)]
    )
    for txn in stuck:
        since = _seconds_since(txn.state_since or txn.updated_at, now)
        row = {"txn": txn, "since_seconds": since, "actions": available_actions(txn), "stale": False}
        if txn.state == State.PAYMENT_CONFIRMED:
            held.append(row)
        elif txn.state == State.PAYOUT_PENDING and since >= stale_after:
            row["stale"] = True
            stale.append(row)
        else:
            grouped[txn.state].append(row)
    return {
        "stale": stale,
        "stale_after": stale_after,
        "held": held,
        "groups": [{"state": s, "label": s.label, "rows": grouped[s]} for s in EXCEPTION_STATES],
        "refund_form": RefundForm(),
        "release_form": ReleaseForm(),
    }


@login_required
def exceptions(request):
    return render(request, "console/exceptions.html", _exceptions_context())


def _treasury_context(*, topup_form=None) -> dict:
    return {
        "float": treasury.float_status(),
        "open_alerts": FloatAlert.objects.filter(acknowledged_at__isnull=True)[:LIST_LIMIT],
        "acknowledged_alerts": FloatAlert.objects.filter(acknowledged_at__isnull=False)
        .select_related("acknowledged_by")
        .order_by("-acknowledged_at")[:10],
        "snapshots": FloatSnapshot.objects.select_related("transaction")[:50],
        "topup_form": topup_form or TopupForm(),
    }


@login_required
def treasury_view(request):
    return render(request, "console/treasury.html", _treasury_context())


@login_required
def payment_methods(request):
    return render(request, "console/methods.html", {"rows": txn_services.wallet_availability()})


def _after_methods_action(request):
    if not _is_htmx(request):
        return redirect("console:methods")
    return render(request, "console/partials/methods_body.html", {"rows": txn_services.wallet_availability()})


# ----------------------------------------------------------------------
# Actions
# ----------------------------------------------------------------------
def _after_transaction_action(request, reference: str, *, refund_form=None, release_form=None):
    if not _is_htmx(request):
        return redirect("console:transaction_detail", reference=reference)
    if request.headers.get("HX-Target") == "exceptions-body":
        return render(request, "console/partials/exceptions_body.html", _exceptions_context())
    txn = get_object_or_404(Transaction, reference=reference)
    return render(
        request,
        "console/partials/transaction_body.html",
        _transaction_context(txn, refund_form=refund_form, release_form=release_form),
    )


def _after_treasury_action(request, *, topup_form=None):
    if not _is_htmx(request):
        return redirect("console:treasury")
    return render(request, "console/partials/treasury_body.html", _treasury_context(topup_form=topup_form))


@login_required
@require_POST
@require_acting_role("payout.retry")
def payout_retry(request, reference: str):
    txn = get_object_or_404(Transaction, reference=reference)
    try:
        txn_services.retry_failed_payout(txn, actor=request.user)
    except IllegalTransition as exc:
        audit_failure(request, "payout.retry", target=reference, error=str(exc))
        messages.error(request, f"Relance refusee : {txn.reference} est en etat « {txn.get_state_display()} ».")
    else:
        messages.success(request, f"{reference} remise en file de decaissement.")
    return _after_transaction_action(request, reference)


@login_required
@require_POST
@require_acting_role("payout.verify")
def payout_verify(request, reference: str):
    txn = get_object_or_404(Transaction, reference=reference)
    before = txn.state
    if before not in (State.PAYOUT_UNKNOWN, State.PAYOUT_PENDING):
        audit_failure(request, "payout.verify", target=reference, error=f"Rien a verifier en etat {before}")
        messages.error(request, f"Rien a verifier : {reference} est en etat « {txn.get_state_display()} ».")
        return _after_transaction_action(request, reference)

    try:
        txn_services.verify_unknown_payout(txn, actor=request.user)
    except IllegalTransition as exc:
        audit_failure(request, "payout.verify", target=reference, error=str(exc))
        messages.error(request, f"Verification interrompue : {exc}")
    else:
        if txn.state == before:
            messages.info(
                request,
                f"{reference} : aucun changement. Retrait toujours en attente chez l'operateur, "
                "ou verification impossible (voir les logs).",
            )
        else:
            messages.success(request, f"{reference} : {State(before).label} → {txn.get_state_display()}.")
    return _after_transaction_action(request, reference)


@login_required
@require_POST
@require_acting_role("transaction.refund", audit_fields=("reason", "transfer_reference", "refunded_amount"))
def transaction_refund(request, reference: str):
    txn = get_object_or_404(Transaction, reference=reference)
    form = RefundForm(request.POST)
    if form.is_valid():
        try:
            txn_services.refund(txn, actor=request.user, **form.cleaned_data)
        except (IllegalTransition, txn_services.InvalidRefund) as exc:
            form.add_error(None, str(exc))

    if form.errors:
        audit_failure(request, "transaction.refund", target=reference, error=_form_errors(form))
        messages.error(request, f"Remboursement NON enregistre : {_form_errors(form)}")
        return _after_transaction_action(request, reference, refund_form=form)

    messages.success(
        request,
        f"Remboursement enregistre pour {reference} (transfert {form.cleaned_data['transfer_reference']}).",
    )
    return _after_transaction_action(request, reference)


@login_required
@require_POST
@require_acting_role("payment.release", audit_fields=("verified_amount", "reason"))
def payment_release(request, reference: str):
    txn = get_object_or_404(Transaction, reference=reference)
    form = ReleaseForm(request.POST)
    if form.is_valid():
        try:
            txn_services.release_held_payment(txn, actor=request.user, **form.cleaned_data)
        except (IllegalTransition, txn_services.InvalidRelease) as exc:
            form.add_error(None, str(exc))

    if form.errors:
        audit_failure(request, "payment.release", target=reference, error=_form_errors(form))
        messages.error(request, f"Deblocage REFUSE : {_form_errors(form)}")
        return _after_transaction_action(request, reference, release_form=form)

    messages.success(request, f"{reference} debloque et mis en file de decaissement.")
    return _after_transaction_action(request, reference)


@login_required
@require_POST
@require_acting_role("float.alert_ack", target_kwarg="alert_id")
def alert_acknowledge(request, alert_id: int):
    alert = get_object_or_404(FloatAlert, pk=alert_id)
    try:
        treasury.acknowledge_alert(alert, user=request.user)
    except treasury.AlertAlreadyAcknowledged as exc:
        audit_failure(request, "float.alert_ack", target=str(alert_id), error=str(exc))
        messages.error(request, "Alerte deja acquittee.")
    else:
        messages.success(request, "Alerte acquittee.")
    return _after_treasury_action(request)


@login_required
@require_POST
@require_superadmin("wallet.availability", target_kwarg="wallet", audit_fields=("direction", "enabled"))
def wallet_availability_update(request, wallet: str):
    direction = request.POST.get("direction", "")
    enabled = request.POST.get("enabled")
    try:
        if enabled not in ("0", "1"):
            raise txn_services.UnsupportedRoute("Valeur attendue : 0 ou 1")
        txn_services.set_wallet_availability(wallet, direction=direction, enabled=enabled == "1", actor=request.user)
    except txn_services.UnsupportedRoute as exc:
        audit_failure(request, "wallet.availability", target=wallet, error=str(exc))
        messages.error(request, f"Reglage refuse : {exc}")
    else:
        sens = "en entree" if direction == "payment" else "en sortie"
        etat = "ouvert" if enabled == "1" else "ferme"
        messages.success(request, f"{wallet} {etat} {sens}. Les transactions deja creees ne sont pas affectees.")
    return _after_methods_action(request)


@login_required
@require_POST
@require_acting_role("float.topup", target_kwarg="", audit_fields=("amount", "reference"))
def float_topup(request):
    form = TopupForm(request.POST)
    if form.is_valid():
        try:
            ledger.record_float_topup(
                form.cleaned_data["amount"],
                reference=form.cleaned_data["reference"],
                posted_by=request.user,
            )
        except ledger.InvalidTopup as exc:
            form.add_error(None, str(exc))

    if form.errors:
        audit_failure(request, "float.topup", error=_form_errors(form))
        messages.error(request, f"Rechargement NON enregistre : {_form_errors(form)}")
        return _after_treasury_action(request, topup_form=form)

    messages.success(
        request,
        f"Rechargement de {form.cleaned_data['amount']} HTG enregistre ({form.cleaned_data['reference']}).",
    )
    return _after_treasury_action(request)
