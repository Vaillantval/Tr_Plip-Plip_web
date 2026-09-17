"""Site client.

Les vues appellent les memes services que l'API (identification, devis,
creation idempotente, statuts publics) : aucune regle metier n'est
dupliquee ici, et aucun modele n'est modifie directement.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from apps.accounts import otp
from apps.accounts import services as accounts
from apps.api import services as api_services
from apps.claims import services as claims
from apps.api.status import MAPPING as STATUS_MAPPING
from apps.api.status import PublicStatus, payment_instructions, public_status, public_wait
from apps.transactions import services as transactions
from apps.transactions.models import Transaction
from apps.transactions.pricing import MIN_NET, AmountTooLarge, AmountTooSmall

from . import ratelimit
from .forms import ClaimForm, ClaimMessageForm, CodeForm, PhoneForm, TransferForm
from .labels import format_htg
from .polling import poll_interval
from .session import customer_required, get_customer, login_customer, logout_customer

logger = logging.getLogger(__name__)

DRAFT_KEY = "web:draft"
PENDING_KEY = "web:pending"
LOGIN_PHONE_KEY = "web:login_phone"
#: Demandes de confirmation gardees en session (une par cle d'idempotence).
MAX_PENDING = 5


def _route_error_message(exc: Exception) -> str:
    if isinstance(exc, transactions.MethodDisabled):
        return _("Ce moyen de paiement est momentanément indisponible. Choisissez-en un autre.")
    if isinstance(exc, AmountTooSmall):
        return _("Le montant minimum est de %(amount)s HTG.") % {"amount": MIN_NET}
    if isinstance(exc, AmountTooLarge):
        return _("Le montant maximum est de %(amount)s HTG.") % {"amount": settings.PRICING["MAX_NET_AMOUNT"]}
    return _("Cette combinaison de portefeuilles n'est pas proposée.")


ROUTE_ERRORS = (transactions.UnsupportedRoute, AmountTooSmall, AmountTooLarge)


def _next_hour(moment):
    """Heure ronde SUPERIEURE, a l'heure de Port-au-Prince.

    Jamais la minute exacte : « a partir de 14 h 32 » dirait au client
    quand sa plus ancienne transaction sort de la fenetre, donc l'heure
    precise a laquelle il a envoye ce jour-la. L'arrondi vers le haut
    evite aussi de promettre plus tot que la realite.
    """
    local = timezone.localtime(moment)
    if local.minute or local.second or local.microsecond:
        local += timedelta(hours=1)
    return local.replace(minute=0, second=0, microsecond=0)


def _saturation_message(saturation) -> str:
    """Pourquoi nous n'acceptons pas, sans reveler la longueur de la file.

    Une duree precise ferait deviner la profondeur de la file, que le
    client ne doit jamais connaitre. « Quelques minutes » suffit.
    """
    if saturation.reason == transactions.ADMISSION_STALLED:
        return _("Les transferts sont momentanément suspendus. Rien n'a été débité. Réessayez plus tard.")
    return _(
        "Nous recevons beaucoup de transferts en ce moment. Plutôt que de prendre votre argent "
        "sans pouvoir le livrer rapidement, nous préférons attendre. Réessayez dans quelques minutes."
    )


def _limit_message(exc) -> str:
    """Plafond atteint : ce que le client peut faire, et quand."""
    cap = format_htg(exc.window.cap)
    if exc.frees_at is None:
        return _("Ce montant depasse a lui seul votre plafond de %(cap)s HTG. Envoyez un montant plus petit.") % {
            "cap": cap
        }

    when = _next_hour(exc.frees_at)
    today = timezone.localdate()
    if when.date() == today:
        tail = _("Vous pourrez envoyer a nouveau a partir de %(hour)s:00.") % {"hour": when.hour}
    elif when.date() == today + timedelta(days=1):
        tail = _("Vous pourrez envoyer a nouveau demain a partir de %(hour)s:00.") % {"hour": when.hour}
    else:
        tail = _("Vous pourrez envoyer a nouveau a partir du %(date)s a %(hour)s:00.") % {
            "date": f"{when.day:02d}/{when.month:02d}",
            "hour": when.hour,
        }

    if exc.window.name == transactions.LIMIT_DAY:
        head = _("Vous avez atteint votre plafond de %(cap)s HTG par jour.") % {"cap": cap}
    else:
        head = _("Vous avez atteint votre plafond de %(cap)s HTG sur 30 jours.") % {"cap": cap}
    return f"{head} {tail}"


def _safe_next(request, default: str) -> str:
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return target
    return default


# ----------------------------------------------------------------------
# Envoi
# ----------------------------------------------------------------------
def home(request):
    wallets = transactions.wallet_availability()
    form = TransferForm(initial=request.session.get(DRAFT_KEY), wallets=wallets)
    # Prevenir des l'accueil plutot qu'apres la saisie complete du
    # formulaire : la lecture passe par la vue de file partagee.
    saturation = transactions.admission_state()
    return render(
        request,
        "web/home.html",
        {
            "form": form,
            "min_net": MIN_NET,
            "saturated": None if saturation.accepting else _saturation_message(saturation),
        },
    )


@require_POST
def quote(request):
    """Fragment HTMX : devis en direct pendant la saisie."""
    if not ratelimit.allow("quote_ip", ratelimit.client_ip(request)):
        return render(request, "web/partials/quote.html", {"error": _("Trop de demandes. Patientez une minute.")})
    try:
        net_amount = Decimal(request.POST.get("net_amount", "").replace(",", "."))
    except InvalidOperation:
        return render(request, "web/partials/quote.html", {})
    try:
        q = transactions.quote_transfer(
            source_wallet=request.POST.get("source_wallet", ""),
            destination_wallet=request.POST.get("destination_wallet", ""),
            net_amount=net_amount,
        )
    except ROUTE_ERRORS as exc:
        return render(request, "web/partials/quote.html", {"error": _route_error_message(exc)})
    return render(request, "web/partials/quote.html", {"quote": q})


@require_POST
def send(request):
    wallets = transactions.wallet_availability()
    form = TransferForm(request.POST, wallets=wallets)
    if form.is_valid():
        data = form.cleaned_data
        try:
            transactions.quote_transfer(
                source_wallet=data["source_wallet"],
                destination_wallet=data["destination_wallet"],
                net_amount=data["net_amount"],
            )
        except ROUTE_ERRORS as exc:
            form.add_error(None, _route_error_message(exc))
    if not form.is_valid():
        return render(request, "web/home.html", {"form": form, "min_net": MIN_NET}, status=400)

    request.session[DRAFT_KEY] = {k: str(v) for k, v in form.cleaned_data.items()}
    return redirect("web:confirm")


@customer_required
@require_http_methods(["GET", "POST"])
def confirm(request):
    if request.method == "POST":
        return _confirm_post(request)

    draft = request.session.get(DRAFT_KEY)
    if not draft:
        return redirect("web:home")
    try:
        q = transactions.quote_transfer(
            source_wallet=draft["source_wallet"],
            destination_wallet=draft["destination_wallet"],
            net_amount=Decimal(draft["net_amount"]),
        )
    except ROUTE_ERRORS as exc:
        messages.error(request, _route_error_message(exc))
        return redirect("web:home")
    return _render_confirm(request, draft, q)


def _render_confirm(request, draft: dict, q, *, status: int = 200):
    # Une cle par page de confirmation : un double envoi du formulaire rejoue
    # la meme cle, et ne cree qu'un seul transfert.
    key = secrets.token_urlsafe(18)
    pending = request.session.get(PENDING_KEY, {})
    pending[key] = draft
    request.session[PENDING_KEY] = dict(list(pending.items())[-MAX_PENDING:])
    return render(
        request,
        "web/confirm.html",
        {"draft": draft, "quote": q, "idempotency_key": key},
        status=status,
    )


def _confirm_post(request):
    key = request.POST.get("idempotency_key", "")
    draft = request.session.get(PENDING_KEY, {}).get(key)
    if draft is None:
        messages.error(request, _("Cette confirmation a expiré. Vérifiez à nouveau votre envoi."))
        return redirect("web:confirm" if request.session.get(DRAFT_KEY) else "web:home")
    try:
        expected_total = Decimal(request.POST.get("expected_total", ""))
    except InvalidOperation:
        messages.error(request, _("Cette confirmation a expiré. Vérifiez à nouveau votre envoi."))
        return redirect("web:confirm")

    if not ratelimit.allow("transfer_create_customer", request.customer.pk):
        messages.error(request, _("Trop de transferts en peu de temps. Réessayez plus tard."))
        return redirect("web:transfers")

    try:
        outcome = api_services.create_transfer(
            customer=request.customer,
            idempotency_key=key,
            source_wallet=draft["source_wallet"],
            destination_wallet=draft["destination_wallet"],
            recipient_phone=draft["recipient_phone"],
            sender_phone=draft.get("sender_phone", ""),
            net_amount=Decimal(draft["net_amount"]),
            expected_total=expected_total,
        )
    except api_services.QuoteChanged as exc:
        messages.warning(request, _("Les frais ont changé depuis votre saisie. Vérifiez le nouveau total avant de confirmer."))
        return _render_confirm(request, draft, exc.quote, status=409)
    except transactions.ServiceSaturated as exc:
        # La saisie du client est bonne : on la garde et on explique.
        messages.error(request, _saturation_message(exc.saturation))
        return redirect("web:home")
    except transactions.LimitExceeded as exc:
        # Contrairement aux autres refus, on garde le client sur sa page de
        # confirmation : sa saisie est bonne, c'est le moment qui ne l'est pas.
        messages.error(request, _limit_message(exc))
        return _render_confirm(
            request,
            draft,
            transactions.quote_transfer(
                source_wallet=draft["source_wallet"],
                destination_wallet=draft["destination_wallet"],
                net_amount=Decimal(draft["net_amount"]),
            ),
            status=409,
        )
    except ROUTE_ERRORS as exc:
        messages.error(request, _route_error_message(exc))
        return redirect("web:home")
    except (api_services.IdempotencyKeyReused, api_services.IdempotencyInProgress):
        messages.error(request, _("Cette demande est déjà en cours de traitement."))
        return redirect("web:transfers")

    if outcome.payment_failed:
        messages.error(request, _("Le paiement n'a pas pu être créé. Aucun montant n'a été débité. Vous pouvez réessayer."))
    request.session.pop(DRAFT_KEY, None)
    return redirect("web:transfer_detail", reference=outcome.transaction.reference)


# ----------------------------------------------------------------------
# Suivi
# ----------------------------------------------------------------------
@customer_required
def transfers(request):
    page = Paginator(Transaction.objects.filter(customer=request.customer).order_by("-created_at", "-id"), 20)
    return render(
        request,
        "web/transfers.html",
        {
            "page": page.get_page(request.GET.get("page")),
            "unread_claims": claims.unread_transaction_ids(request.customer),
        },
    )


def _own_transfer(request, reference: str) -> Transaction:
    # 404 pour la transaction d'un autre client : ne pas confirmer qu'elle existe.
    txn = Transaction.objects.filter(customer=request.customer, reference=reference).first()
    if txn is None:
        raise Http404
    return txn


def _transfer_context(txn: Transaction) -> dict:
    status = public_status(txn.state)
    return {
        "txn": txn,
        "status": status,
        "poll_seconds": poll_interval(txn, status),
        "payment": payment_instructions(txn, status),
        "wait": public_wait(txn),
    }


@customer_required
def transfer_detail(request, reference: str):
    txn = _own_transfer(request, reference)
    context = _transfer_context(txn)
    claim = claims.active_claim(txn)
    if claim is not None:
        # Ouvrir la page vaut lecture : la pastille s'eteint ici.
        claims.mark_seen(claim)
        context |= {"claim": claim, "claim_messages": claim.messages.all(), "message_form": ClaimMessageForm()}
    return render(request, "web/transfer_detail.html", context)


@customer_required
def transfer_status(request, reference: str):
    """Fragment HTMX rafraichi tant que le transfert n'est pas termine."""
    return render(request, "web/partials/transfer_status.html", _transfer_context(_own_transfer(request, reference)))


# ----------------------------------------------------------------------
# Reclamations
# ----------------------------------------------------------------------
@customer_required
@require_http_methods(["GET", "POST"])
def claim_open(request, reference: str):
    """Ouvrir une reclamation sur UN DE SES transferts.

    _own_transfer renvoie 404 pour la transaction d'un autre : ne pas
    confirmer qu'elle existe.
    """
    txn = _own_transfer(request, reference)
    existing = claims.active_claim(txn)
    if existing is not None:
        return redirect("web:transfer_detail", reference=txn.reference)

    form = ClaimForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not ratelimit.allow("claim_create_customer", request.customer.pk):
            form.add_error(None, _("Trop de réclamations en peu de temps. Réessayez plus tard."))
        else:
            try:
                claims.open_claim(
                    customer=request.customer,
                    transaction=txn,
                    reason=form.cleaned_data["reason"],
                    body=form.cleaned_data["body"],
                )
            except claims.ClaimError as exc:
                form.add_error(None, str(exc.message))
            else:
                messages.success(
                    request,
                    _("Votre réclamation est enregistrée. Nous vous répondons sur cette page, et par SMS."),
                )
                return redirect("web:transfer_detail", reference=txn.reference)
    return render(request, "web/claim_open.html", {"txn": txn, "form": form}, status=400 if form.errors else 200)


@customer_required
@require_POST
def claim_message(request, reference: str):
    txn = _own_transfer(request, reference)
    claim = claims.active_claim(txn)
    if claim is None:
        raise Http404
    form = ClaimMessageForm(request.POST)
    if not form.is_valid():
        # Le texte vient de l'exception metier, jamais d'une copie locale :
        # le site et l'API doivent dire la meme chose au client.
        messages.error(request, str(claims.InvalidBody().message))
    elif not ratelimit.allow("claim_message_customer", request.customer.pk):
        messages.error(request, _("Trop de messages en peu de temps. Réessayez plus tard."))
    else:
        try:
            claims.add_customer_message(customer=request.customer, claim=claim, body=form.cleaned_data["body"])
        except claims.ClaimError as exc:
            messages.error(request, str(exc.message))
        else:
            messages.success(request, _("Message ajouté à votre réclamation."))
    return redirect("web:transfer_detail", reference=txn.reference)


# ----------------------------------------------------------------------
# Retour depuis plopplop
# ----------------------------------------------------------------------
#: Le retour n'est pas documente par plopplop : noms de parametres plausibles
#: pour notre reference, puis pour leur identifiant de transaction.
RETURN_REFERENCE_PARAMS = ("refference_id", "reference_id", "reference", "ref")
RETURN_PROVIDER_ID_PARAMS = ("transaction_id", "id_transaction")
#: Sans parametre exploitable : dernier transfert encore suivi, de moins de 2 h.
RETURN_FALLBACK_WINDOW = timedelta(hours=2)
RETURN_FALLBACK_STATES = [
    state for state, public in STATUS_MAPPING.items() if public in (PublicStatus.AWAITING_PAYMENT, PublicStatus.IN_PROGRESS)
]


def payment_return(request):
    """URL de retour saisie dans l'espace marchand plopplop : /paiement/retour/.

    Ce retour ne prouve RIEN : aucun etat ne change ici et plopplop n'est
    pas appele -- n'importe qui peut ouvrir cette adresse. Seul le polling
    de paiement-verify confirme un paiement. On emmene simplement le client
    sur la page de suivi de SON transfert, qui se met a jour seule.
    """
    # Noms seulement : sert a decouvrir le format reel du retour plopplop.
    logger.info("Retour plopplop, parametres recus : %s", sorted(request.GET.keys()))
    customer = get_customer(request)
    if customer is None:
        return redirect(f"{reverse('web:login')}?{urlencode({'next': request.get_full_path()})}")
    txn = _returned_transfer(customer, request.GET)
    if txn is None:
        return redirect("web:transfers")
    return redirect("web:transfer_detail", reference=txn.reference)


def _returned_transfer(customer, params) -> Transaction | None:
    own = Transaction.objects.filter(customer=customer)
    lookups = [("reference", name) for name in RETURN_REFERENCE_PARAMS] + [
        ("payment_provider_id", name) for name in RETURN_PROVIDER_ID_PARAMS
    ]
    for field, name in lookups:
        value = params.get(name, "").strip()
        if value:
            txn = own.filter(**{field: value}).first()
            if txn is not None:
                return txn
    return (
        own.filter(state__in=RETURN_FALLBACK_STATES, created_at__gte=timezone.now() - RETURN_FALLBACK_WINDOW)
        .order_by("-created_at", "-id")
        .first()
    )


# ----------------------------------------------------------------------
# Connexion
# ----------------------------------------------------------------------
@require_http_methods(["GET", "POST"])
def login(request):
    next_url = _safe_next(request, reverse("web:transfers"))
    if get_customer(request) is not None:
        return redirect(next_url)
    form = PhoneForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        phone = form.cleaned_data["phone"]
        ip = ratelimit.client_ip(request)
        allowed = (
            ratelimit.allow("otp_request_phone_burst", phone)
            and ratelimit.allow("otp_request_phone", phone)
            and ratelimit.allow("otp_request_ip", ip)
        )
        if not allowed:
            form.add_error(None, _("Trop de demandes de code pour ce numéro. Patientez avant de réessayer."))
        else:
            try:
                accounts.request_code(phone)
            except otp.OTPRateLimited:
                form.add_error(None, _("Trop de demandes de code pour ce numéro. Patientez avant de réessayer."))
            except otp.OTPInvalidPhone:
                form.add_error("phone", _("Ce numéro ne peut pas recevoir de SMS."))
            except otp.OTPUnavailable:
                form.add_error(None, _("L'envoi du SMS est momentanément impossible. Réessayez dans quelques minutes."))
            else:
                request.session[LOGIN_PHONE_KEY] = phone
                return redirect(f"{reverse('web:login_code')}?{urlencode({'next': next_url})}")
    return render(request, "web/login.html", {"form": form, "next": next_url})


@require_http_methods(["GET", "POST"])
def login_code(request):
    next_url = _safe_next(request, reverse("web:transfers"))
    phone = request.session.get(LOGIN_PHONE_KEY)
    if not phone:
        return redirect("web:login")
    form = CodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if not ratelimit.allow("otp_verify_phone", phone):
            form.add_error(None, _("Trop de tentatives. Demandez un nouveau code plus tard."))
        else:
            try:
                # La langue active pour cette requete est celle que le
                # client est en train de lire : la seule dont disposera
                # ensuite une tache Celery pour lui ecrire.
                customer = accounts.authenticate_code(
                    phone, form.cleaned_data["code"], language=get_language()
                )
            except accounts.InvalidCode:
                form.add_error("code", _("Code invalide ou expiré."))
            except accounts.CustomerDisabled:
                form.add_error(None, _("Ce compte est désactivé. Contactez le support."))
            except otp.OTPRateLimited:
                form.add_error(None, _("Trop de tentatives. Demandez un nouveau code."))
            except otp.OTPUnavailable:
                form.add_error(None, _("La vérification est momentanément impossible. Réessayez dans quelques minutes."))
            else:
                # cycle_key() conserve la session : le brouillon d'envoi survit.
                login_customer(request, customer)
                request.session.pop(LOGIN_PHONE_KEY, None)
                return redirect(next_url)
    return render(request, "web/login_code.html", {"form": form, "phone": phone, "next": next_url})


@require_POST
def logout(request):
    logout_customer(request)
    return redirect("web:home")
