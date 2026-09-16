"""Site web client.

Regles protegees : session client isolee de la console, idempotence de la
confirmation, methodes fermees absentes et refusees, statuts publics,
devis confirme, limites de debit, langues.
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import translation

from apps.accounts.models import Customer, Role, User
from apps.ledger import services as ledger
from apps.providers.plopplop import exceptions as pp
from apps.providers.plopplop.client import PaymentIntent
from apps.transactions import services
from apps.transactions.models import Transaction
from apps.transactions.states import State

GET_CLIENT = "apps.transactions.services.get_client"
CODE = "123456"
PHONE = "50937123456"
OTHER = "50948123456"
LOCALE_DIR = Path(__file__).resolve().parent.parent / "apps" / "web" / "locale"

SEND = {
    "source_wallet": "moncash",
    "destination_wallet": "natcash",
    "recipient_phone": "3212 3456",
    "net_amount": "1000",
    "sender_phone": "",
}


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


def _login(client, phone=PHONE, next_url=None):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:login"), {"phone": phone})
    url = reverse("web:login_code") + (f"?next={next_url}" if next_url else "")
    return client.post(url, {"code": CODE})


def _confirm_page(client):
    client.post(reverse("web:send"), SEND)
    return client.get(reverse("web:confirm"))


def _hidden(response, name):
    return re.search(rf'name="{name}" value="([^"]+)"', response.content.decode()).group(1)


def _intent(url="https://pay.example/abc"):
    return PaymentIntent(transaction_id="PAY-1", reference="PP", redirect_url=url)


# ----------------------------------------------------------------------
# Connexion et isolation
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_sms_login_opens_a_customer_session(client):
    response = _login(client)

    assert response.status_code == 302 and response["Location"] == reverse("web:transfers")
    customer = Customer.objects.get(phone=PHONE)
    assert client.session["web:customer_id"] == customer.pk
    assert client.get(reverse("web:transfers")).status_code == 200


@pytest.mark.django_db
def test_wrong_code_does_not_log_in(client):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:login"), {"phone": PHONE})
    response = client.post(reverse("web:login_code"), {"code": "000000"})

    assert response.status_code == 200
    assert "web:customer_id" not in client.session
    assert client.get(reverse("web:transfers")).status_code == 302


@pytest.mark.django_db
def test_customer_session_does_not_open_the_console(client):
    _login(client)
    response = client.get(reverse("console:dashboard"))
    assert response.status_code == 302 and reverse("console:login") in response["Location"]


@pytest.mark.django_db
def test_staff_session_does_not_open_the_customer_site(client):
    client.force_login(User.objects.create_user(username="sa", password="x", role=Role.SUPERADMIN))
    response = client.get(reverse("web:transfers"))
    assert response.status_code == 302 and response["Location"].startswith(reverse("web:login"))


@pytest.mark.django_db
def test_login_rotates_the_session_key(client):
    client.get(reverse("web:home"))
    client.post(reverse("web:send"), SEND)
    before = client.session.session_key

    _login(client)

    assert client.session.session_key != before
    assert client.session["web:draft"]["net_amount"] == "1000"  # le brouillon survit


@pytest.mark.django_db
def test_idle_customer_session_expires(client, settings):
    settings.WEB_SESSION_IDLE_SECONDS = 60
    _login(client)
    session = client.session
    session["web:last_seen"] -= 120
    session.save()

    assert client.get(reverse("web:transfers")).status_code == 302
    assert "web:customer_id" not in client.session


@pytest.mark.django_db
def test_sms_requests_are_rate_limited_per_phone(client):
    first = client.post(reverse("web:login"), {"phone": PHONE})
    second = client.post(reverse("web:login"), {"phone": "3712-3456"})

    assert first.status_code == 302
    assert second.status_code == 200 and "Trop de demandes de code" in second.content.decode()


@pytest.mark.django_db
def test_open_redirect_is_refused_after_login(client):
    response = _login(client, next_url="https://evil.example/")
    assert response["Location"] == reverse("web:transfers")


# ----------------------------------------------------------------------
# Envoi
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_live_quote_shows_the_detailed_fees(client):
    response = client.post(reverse("web:quote"), {k: SEND[k] for k in ("source_wallet", "destination_wallet", "net_amount")})
    html = response.content.decode()
    assert "1 090,00 HTG" in html and "Frais MonCash" in html and "Frais Plip-Plip" in html


@pytest.mark.django_db
def test_closed_methods_are_not_offered_and_are_refused_if_forced(client):
    services.set_wallet_availability("kashpaw", direction="payment", enabled=False)

    home = client.get(reverse("web:home")).content.decode()
    forced = client.post(reverse("web:send"), {**SEND, "source_wallet": "kashpaw"})

    assert 'value="kashpaw"' not in home and 'value="carte"' in home
    assert forced.status_code == 400
    assert "web:draft" not in client.session


@pytest.mark.django_db
def test_sending_while_logged_out_goes_through_login_then_confirmation(client):
    client.post(reverse("web:send"), SEND)
    to_confirm = client.get(reverse("web:confirm"))
    assert to_confirm.status_code == 302 and to_confirm["Location"].startswith(reverse("web:login"))

    response = _login(client, next_url=reverse("web:confirm"))

    assert response["Location"] == reverse("web:confirm")
    page = client.get(reverse("web:confirm")).content.decode()
    assert "Payer 1 090,00 HTG" in page and "+50932123456" not in page and "50932123456" in page


@pytest.mark.django_db
def test_double_submit_of_the_confirmation_creates_a_single_transfer(client):
    _login(client)
    page = _confirm_page(client)
    body = {"idempotency_key": _hidden(page, "idempotency_key"), "expected_total": _hidden(page, "expected_total")}

    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.create_payment.return_value = _intent()
        first = client.post(reverse("web:confirm"), body)
        second = client.post(reverse("web:confirm"), body)

    txn = Transaction.objects.get()
    assert first["Location"] == second["Location"] == reverse("web:transfer_detail", args=[txn.reference])
    get_client.return_value.create_payment.assert_called_once()
    assert txn.customer.phone == PHONE and txn.recipient_phone == "50932123456"


@pytest.mark.django_db
def test_changed_fees_require_a_new_confirmation(client):
    _login(client)
    page = _confirm_page(client)
    body = {"idempotency_key": _hidden(page, "idempotency_key"), "expected_total": "1080.00"}

    with mock.patch(GET_CLIENT) as get_client:
        response = client.post(reverse("web:confirm"), body)

    assert response.status_code == 409
    assert "Les frais ont changé" in response.content.decode()
    assert _hidden(response, "expected_total") == "1090.00"
    get_client.return_value.create_payment.assert_not_called()
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_forged_idempotency_key_is_not_accepted(client):
    _login(client)
    _confirm_page(client)
    with mock.patch(GET_CLIENT) as get_client:
        response = client.post(reverse("web:confirm"), {"idempotency_key": "invente-par-le-client", "expected_total": "1090.00"})
    assert response.status_code == 302
    get_client.return_value.create_payment.assert_not_called()
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_payment_provider_refusal_is_reported_without_charging(client):
    _login(client)
    page = _confirm_page(client)
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.create_payment.side_effect = pp.MethodNotConfigured("inactif", code="METHOD_NOT_CONFIGURED", status=400)
        response = client.post(
            reverse("web:confirm"),
            {"idempotency_key": _hidden(page, "idempotency_key"), "expected_total": _hidden(page, "expected_total")},
            follow=True,
        )
    html = response.content.decode()
    assert "Aucun montant n'a été débité" in html
    assert Transaction.objects.get().state == State.CANCELLED


# ----------------------------------------------------------------------
# Suivi
# ----------------------------------------------------------------------
def _create_for(customer_phone, *, intent=None):
    customer, _ = Customer.objects.get_or_create(phone=customer_phone)
    from apps.api import services as api_services

    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.create_payment.return_value = intent or _intent()
        return api_services.create_transfer(
            customer=customer, idempotency_key=f"cle-{customer_phone}", source_wallet="moncash",
            destination_wallet="natcash", recipient_phone="50932123456", net_amount=Decimal("1000"),
            expected_total=Decimal("1090.00"),
        ).transaction


@pytest.mark.django_db
def test_transfer_page_shows_payment_link_and_keeps_polling(client):
    txn = _create_for(PHONE)
    _login(client)

    html = client.get(reverse("web:transfer_detail", args=[txn.reference])).content.decode()

    assert txn.reference in html
    assert 'href="https://pay.example/abc"' in html and 'rel="noopener noreferrer"' in html
    assert 'hx-trigger="every 5s"' in html


@pytest.mark.django_db
def test_internal_payout_trouble_is_shown_in_progress_and_polling_stops_when_delivered(client):
    ledger.ensure_accounts()
    txn = _create_for(PHONE)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.side_effect = pp.PlopPlopIndeterminate("timeout")
        services.execute_payout(txn)
    _login(client)
    status_url = reverse("web:transfer_status", args=[txn.reference])

    html = client.get(status_url).content.decode()
    assert "En cours" in html and "unknown" not in html.lower() and 'hx-trigger="every 5s"' in html

    with mock.patch(GET_CLIENT) as get_client:
        from apps.providers.plopplop.client import WithdrawalStatus

        txn.refresh_from_db()
        get_client.return_value.withdrawal_status.return_value = WithdrawalStatus(
            reference=txn.payout_reference, status="success", transaction_id="WD", amount=None
        )
        services.verify_unknown_payout(txn)
    delivered = client.get(status_url).content.decode()
    assert "Livré" in delivered and "hx-trigger" not in delivered


@pytest.mark.django_db
def test_customer_never_sees_another_customers_transfer(client):
    other = _create_for(OTHER)
    _login(client)

    assert client.get(reverse("web:transfer_detail", args=[other.reference])).status_code == 404
    assert client.get(reverse("web:transfer_status", args=[other.reference])).status_code == 404
    assert other.reference not in client.get(reverse("web:transfers")).content.decode()


@pytest.mark.django_db
def test_unsafe_payment_url_is_never_rendered_as_a_link(client):
    txn = _create_for(PHONE, intent=_intent(url="javascript:alert(1)"))
    _login(client)
    html = client.get(reverse("web:transfer_detail", args=[txn.reference])).content.decode()
    assert "javascript:" not in html


# ----------------------------------------------------------------------
# Langues
# ----------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize(
    "language, expected",
    [
        ("fr", "Envoyer de l'argent d'un portefeuille à l'autre"),
        ("en", "Send money from one wallet to another"),
        ("ht", "Voye kòb soti nan yon pòtfèy al nan yon lòt"),
    ],
)
def test_home_page_in_each_language(client, language, expected):
    client.post(reverse("set_language"), {"language": language, "next": "/"})
    html = client.get(reverse("web:home")).content.decode()
    assert expected in html.replace("&#x27;", "'")
    assert f'<html lang="{language}">' in html


@pytest.mark.django_db
def test_french_is_the_default_language(client):
    html = client.get(reverse("web:home"), HTTP_ACCEPT_LANGUAGE="de").content.decode()
    assert '<html lang="fr">' in html


#: Seuls fichiers dont les chaines creoles peuvent attendre (A_TRADUIRE.md).
#: Tout le reste est le chemin de l'argent : montants, frais, confirmation,
#: erreurs, statut. Un nouveau fichier bloque par defaut.
DEFERRABLE_FILES = {
    "apps/web/templates/web/base.html",
    "apps/web/templates/web/login.html",
    "apps/web/templates/web/login_code.html",
}
TO_TRANSLATE = LOCALE_DIR / "ht" / "A_TRADUIRE.md"


def _catalog(language):
    """Entrees actives d'un catalogue .po : msgid, msgstr, fichiers, drapeaux."""
    import ast

    text = (LOCALE_DIR / language / "LC_MESSAGES" / "django.po").read_text(encoding="utf-8")
    entries = []
    for block in re.split(r"\n\s*\n", text):
        locations, flags, parts, current, obsolete = set(), [], {"msgid": [], "msgstr": []}, None, False
        for line in block.splitlines():
            if line.startswith("#~"):
                obsolete = True
            elif line.startswith("#:"):
                # Windows ecrit « .\apps\web\... », Linux « apps/web/... ».
                locations |= {loc.replace("\\", "/").removeprefix("./").split(":")[0] for loc in line[2:].split()}
            elif line.startswith("#,"):
                flags += [flag.strip() for flag in line[2:].split(",")]
            elif line.startswith(("msgid ", "msgstr ")):
                current, _, rest = line.partition(" ")
                parts[current].append(ast.literal_eval(rest))
            elif line.startswith('"') and current:
                parts[current].append(ast.literal_eval(line))
        msgid = "".join(parts["msgid"])
        if obsolete or not msgid:
            continue
        entries.append({"msgid": msgid, "msgstr": "".join(parts["msgstr"]), "locations": locations, "flags": flags})
    return entries


def _deferred():
    block = re.search(r"```text\n(.*?)```", TO_TRANSLATE.read_text(encoding="utf-8"), re.S)
    return [line for line in block.group(1).splitlines() if line.strip()]


@pytest.mark.parametrize("language", ["en", "ht"])
def test_translation_catalogs_are_complete_and_compiled(language):
    po = LOCALE_DIR / language / "LC_MESSAGES" / "django.po"
    mo = po.with_suffix(".mo")
    entries = _catalog(language)
    assert len(entries) > 80
    assert [e["msgid"] for e in entries if "fuzzy" in e["flags"]] == []

    # Anglais : complet. Creole : seules les chaines listees dans
    # A_TRADUIRE.md peuvent attendre (voir les deux tests suivants).
    untranslated = {e["msgid"] for e in entries if not e["msgstr"]}
    allowed = set(_deferred()) if language == "ht" else set()
    assert sorted(untranslated - allowed) == []
    assert mo.exists() and mo.stat().st_mtime >= po.stat().st_mtime - 1

    with translation.override(language):
        assert translation.gettext("Mes transferts") != "Mes transferts"


def test_money_path_strings_can_never_wait_for_creole():
    entries = {e["msgid"]: e for e in _catalog("ht")}
    assert all(e["locations"] for e in entries.values()), "makemessages doit garder les emplacements (--add-location file)"
    blocking = sorted(m for m in _deferred() if m in entries and not entries[m]["locations"] <= DEFERRABLE_FILES)
    assert blocking == []


def test_creole_to_do_list_has_no_stale_entries():
    entries = {e["msgid"]: e for e in _catalog("ht")}
    listed = _deferred()
    assert len(listed) == len(set(listed))
    stale = sorted(m for m in listed if m not in entries or entries[m]["msgstr"])
    assert stale == []
