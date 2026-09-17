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
from django.db.models import Case, Count, IntegerField, Q, Sum, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.claims import services as claims
from apps.claims.models import Claim
from apps.claims.models import Status as ClaimStatus
from apps.ledger import services as ledger
from apps.transactions import queue
from apps.transactions import services as txn_services
from apps.accounts.models import Customer
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import IllegalTransition, State, can
from apps.treasury import services as treasury
from apps.treasury.models import FloatAlert, FloatSnapshot

from .forms import (
    RATE_INPUTS,
    ClaimAnswerForm,
    ClaimCloseForm,
    LimitsForm,
    PricingForm,
    RefundForm,
    ReleaseForm,
    TopupForm,
    pricing_field_name,
)
from .permissions import audit_failure, require_acting_role, require_superadmin

EXCEPTION_STATES = (
    State.PAYOUT_FAILED,
    State.PAYOUT_UNKNOWN,
    State.PAYOUT_PENDING,
    # Visible pour qu'un orphelin ne s'accumule pas en silence si le
    # balayage automatique ne tourne plus.
    State.PAYOUT_IN_FLIGHT,
)

#: Etats ou la duree passee dans l'etat vaut incident, et le reglage qui
#: en fixe le seuil. Toute entree doit etre dans EXCEPTION_STATES.
STALE_AFTER = {
    State.PAYOUT_PENDING: "PAYOUT_PENDING_STALE_SECONDS",
    State.PAYOUT_IN_FLIGHT: "PAYOUT_INFLIGHT_STALE_SECONDS",
}
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
        "pricing": txn_services.pricing_overview(),
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
    qs = Transaction.objects.select_related("customer").order_by("-created_at", "-id")
    state = request.GET.get("state")
    customer_phone = (request.GET.get("customer") or "").strip()
    if state:
        qs = qs.filter(state=state)
    if customer_phone:
        qs = qs.filter(customer__phone=customer_phone)
    customer = Customer.objects.filter(phone=customer_phone).first() if customer_phone else None
    return render(
        request,
        "console/transactions.html",
        {
            "transactions": qs[:LIST_LIMIT],
            "states": State.choices,
            "current_state": state,
            "customer": customer,
            "customer_phone": customer_phone,
            "consumption": _consumption_rows(customer) if customer else None,
        },
    )


WINDOW_LABELS = {txn_services.LIMIT_DAY: "24 dernieres heures", txn_services.LIMIT_MONTH: "30 derniers jours"}


def _consumption_rows(customer) -> list[dict]:
    return [
        {
            "label": WINDOW_LABELS.get(w.name, w.name),
            "collected": w.collected,
            "reserved": w.reserved,
            "used": w.used,
            "cap": w.cap,
            "remaining": w.remaining,
        }
        for w in txn_services.customer_consumption(customer)
    ]


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
        "consumption": _consumption_rows(txn.customer) if txn.customer_id else None,
        # Ce que le client dit de ce transfert, a lire avant d'agir dessus.
        "claims": txn.claims.all(),
    }


@login_required
def transaction_detail(request, reference: str):
    txn = get_object_or_404(Transaction, reference=reference)
    return render(request, "console/transaction_detail.html", _transaction_context(txn))


def _queue_context() -> dict:
    snapshot = queue.queue_snapshot()
    now = snapshot.taken_at
    cooldown = settings.PAYOUT_COOLDOWN_SECONDS
    coverage = snapshot.coverage
    rows = [
        {
            "rank": entry.rank,
            "txn": entry.txn,
            "wait_seconds": _seconds_since(entry.txn.payment_confirmed_at, now),
            "eta_seconds": entry.eta_seconds,
            "covered": entry.covered,
            "stalls_here": entry.rank == coverage["stall_rank"],
        }
        for entry in snapshot.entries[:LIST_LIMIT]
    ]
    return {
        "rows": rows,
        "coverage": coverage,
        "depth": coverage["depth"],
        "cooldown": cooldown,
        "drain_seconds": coverage["depth"] * cooldown,
        "truncated": coverage["depth"] > len(rows),
        "worker": snapshot.worker,
        "now": now,
    }


@login_required
def payout_queue(request):
    """L'ecran que l'operateur garde ouvert : ce qui attend d'etre verse."""
    template = "console/partials/queue_body.html" if _is_htmx(request) else "console/queue.html"
    return render(request, template, _queue_context())


def _exceptions_context() -> dict:
    now = timezone.now()
    thresholds = {state: getattr(settings, name) for state, name in STALE_AFTER.items()}
    grouped = {state: [] for state in EXCEPTION_STATES}
    held = []
    stale = []
    orphans = []
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
        threshold = thresholds.get(txn.state)
        if txn.state == State.PAYMENT_CONFIRMED:
            held.append(row)
        elif threshold is not None and since >= threshold:
            row["stale"] = True
            (stale if txn.state == State.PAYOUT_PENDING else orphans).append(row)
        else:
            grouped[txn.state].append(row)
    return {
        "stale": stale,
        "stale_after": thresholds[State.PAYOUT_PENDING],
        "orphans": orphans,
        "orphan_after": thresholds[State.PAYOUT_IN_FLIGHT],
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


def _percent(rate) -> Decimal:
    return (rate * 100).quantize(Decimal("0.01"))


def _limits_context(*, limits_form=None) -> dict:
    overview = txn_services.limits_overview()
    return {
        "limits": overview,
        "limits_form": limits_form
        or LimitsForm(
            initial={"daily_cap": overview["policy"].daily_cap, "monthly_cap": overview["policy"].monthly_cap}
        ),
    }


def _methods_context(*, pricing_form=None, limits_form=None) -> dict:
    overview = txn_services.pricing_overview()
    if pricing_form is None:
        initial = {"platform_fee_rate": _percent(overview["policy"].platform_fee_rate)}
        for wallet in overview["wallets"]:
            for field, _, _ in RATE_INPUTS:
                initial[pricing_field_name(wallet["wallet"], field)] = _percent(wallet[field])
        pricing_form = PricingForm(initial=initial, wallets=overview["wallets"])
    rate_rows = [
        {
            "wallet": wallet,
            "cells": [
                {
                    "field": field,
                    "label": label,
                    "applicable": wallet["payout_capable"] or not payout_only,
                    "bound": pricing_form[pricing_field_name(wallet["wallet"], field)]
                    if wallet["payout_capable"] or not payout_only
                    else None,
                    "percent": _percent(wallet[field]),
                }
                for field, label, payout_only in RATE_INPUTS
            ],
        }
        for wallet in overview["wallets"]
    ]
    return {
        "rows": overview["wallets"],
        "pricing": overview,
        "pricing_form": pricing_form,
        "rate_rows": rate_rows,
        "rate_labels": [label for _, label, _ in RATE_INPUTS],
        **_limits_context(limits_form=limits_form),
    }


@login_required
def payment_methods(request):
    return render(request, "console/methods.html", _methods_context())


def _after_methods_action(request, *, pricing_form=None, limits_form=None):
    if not _is_htmx(request):
        return redirect("console:methods")
    return render(
        request,
        "console/partials/methods_body.html",
        _methods_context(pricing_form=pricing_form, limits_form=limits_form),
    )


PRICING_AUDIT_FIELDS = ("platform_fee_rate",) + tuple(
    pricing_field_name(wallet, field) for wallet in Wallet.values for field, _, _ in RATE_INPUTS
)


@login_required
@require_POST
@require_superadmin("limits.update", audit_fields=("daily_cap", "monthly_cap"))
def limits_update(request):
    """Plafonds cumules par client. Superadmin uniquement."""
    form = LimitsForm(request.POST)
    if form.is_valid():
        try:
            txn_services.update_transfer_limits(**form.cleaned_data, actor=request.user)
        except txn_services.InvalidLimits as exc:
            form.add_error(None, str(exc))

    if form.errors:
        audit_failure(request, "limits.update", error=_form_errors(form))
        messages.error(request, f"Plafonds NON enregistres : {_form_errors(form)}")
        return _after_methods_action(request, limits_form=form)

    messages.success(
        request,
        "Plafonds enregistres. Ils s'appliquent aux NOUVELLES creations ; "
        "aucune transaction en cours n'est touchee.",
    )
    return _after_methods_action(request)


@login_required
@require_POST
@require_superadmin("pricing.update", audit_fields=PRICING_AUDIT_FIELDS)
def pricing_update(request):
    form = PricingForm(request.POST, wallets=txn_services.wallet_availability())
    if form.is_valid():
        platform_fee_rate, wallet_rates = form.rates()
        try:
            txn_services.update_pricing(
                platform_fee_rate=platform_fee_rate, wallet_rates=wallet_rates, actor=request.user
            )
        except txn_services.InvalidPricing as exc:
            form.add_error(None, str(exc))

    if form.errors:
        audit_failure(request, "pricing.update", error=_form_errors(form))
        messages.error(request, f"Tarification NON enregistree : {_form_errors(form)}")
        return _after_methods_action(request, pricing_form=form)

    messages.success(
        request,
        "Tarification enregistree et validee. Elle s'applique aux nouveaux devis ; "
        "les transactions deja creees gardent leur devis.",
    )
    return _after_methods_action(request)


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


# ----------------------------------------------------------------------
# Reclamations
# ----------------------------------------------------------------------
#: Les ouvertes d'abord, les plus anciennes en tete : une reclamation qui
#: vieillit est l'information utile, pas la derniere arrivee.
CLAIM_ORDER = ("status_rank", "created_at", "id")


def _claims_context(*, answer_form=None, close_form=None, claim=None) -> dict:
    ranked = Claim.objects.annotate(
        status_rank=Case(
            When(status=ClaimStatus.OPEN, then=0),
            When(status=ClaimStatus.ANSWERED, then=1),
            default=2,
            output_field=IntegerField(),
        )
    ).select_related("transaction", "customer")
    return {
        "claims": ranked.order_by(*CLAIM_ORDER)[:LIST_LIMIT],
        "open_count": Claim.objects.filter(status=ClaimStatus.OPEN).count(),
        "claim": claim,
        "claim_messages": claim.messages.select_related("author") if claim else None,
        "answer_form": answer_form or ClaimAnswerForm(),
        "close_form": close_form or ClaimCloseForm(),
    }


@login_required
def claim_list(request):
    template = "console/partials/claims_body.html" if _is_htmx(request) else "console/claims.html"
    return render(request, template, _claims_context())


@login_required
def claim_detail(request, claim_id: int):
    claim = get_object_or_404(Claim.objects.select_related("transaction", "customer"), pk=claim_id)
    return render(request, "console/claim_detail.html", _claims_context(claim=claim))


def _after_claim_action(request, claim_id: int, *, answer_form=None, close_form=None):
    if not _is_htmx(request):
        return redirect("console:claim_detail", claim_id=claim_id)
    claim = get_object_or_404(Claim, pk=claim_id)
    return render(
        request,
        "console/partials/claim_body.html",
        _claims_context(claim=claim, answer_form=answer_form, close_form=close_form),
    )


@login_required
@require_POST
@require_acting_role("claim.answer", target_kwarg="claim_id", audit_fields=("body",))
def claim_answer(request, claim_id: int):
    """Repondre. Ne touche NI a l'etat de la transaction, NI au grand livre :
    pour rembourser, l'operateur passe par le detail du transfert."""
    claim = get_object_or_404(Claim, pk=claim_id)
    form = ClaimAnswerForm(request.POST)
    if form.is_valid():
        try:
            claims.answer_claim(claim=claim, actor=request.user, body=form.cleaned_data["body"])
        except claims.ClaimError as exc:
            form.add_error(None, str(exc.message))
    if form.errors:
        audit_failure(request, "claim.answer", target=str(claim_id), error=_form_errors(form))
        messages.error(request, f"Reponse NON enregistree : {_form_errors(form)}")
        return _after_claim_action(request, claim_id, answer_form=form)
    messages.success(request, f"Reponse envoyee au client pour {claim.transaction.reference}.")
    return _after_claim_action(request, claim_id)


@login_required
@require_POST
@require_acting_role("claim.close", target_kwarg="claim_id", audit_fields=("note",))
def claim_close(request, claim_id: int):
    claim = get_object_or_404(Claim, pk=claim_id)
    form = ClaimCloseForm(request.POST)
    if form.is_valid():
        try:
            claims.close_claim(claim=claim, actor=request.user, note=form.cleaned_data["note"])
        except claims.ClaimError as exc:
            form.add_error(None, str(exc.message))
    if form.errors:
        audit_failure(request, "claim.close", target=str(claim_id), error=_form_errors(form))
        messages.error(request, f"Cloture refusee : {_form_errors(form)}")
        return _after_claim_action(request, claim_id, close_form=form)
    messages.success(request, f"Reclamation close pour {claim.transaction.reference}.")
    return _after_claim_action(request, claim_id)


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
