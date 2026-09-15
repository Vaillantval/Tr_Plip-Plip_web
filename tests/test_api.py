"""Tests de l'API publique.

Chaque test protege une regle : identification, isolation des donnees,
idempotence, devis confirme, statuts publics, methodes ouvertes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
import responses
from django.core.cache import cache
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import services as accounts
from apps.accounts.models import AuditLog, Customer, CustomerToken, Role, User
from apps.accounts.phone import InvalidPhone, normalize
from apps.ledger import services as ledger
from apps.providers.plopplop import exceptions as pp
from apps.providers.plopplop.client import PaymentIntent, PaymentStatus, WithdrawalResult
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

APPS_DIR = Path(__file__).resolve().parent.parent / "apps"
GET_CLIENT = "apps.transactions.services.get_client"
CODE = "123456"
PHONE = "50937123456"
OTHER_PHONE = "50948123456"
RECIPIENT = "50932123456"
TWILIO_SERVICE = "VA0000000000000000000000000000test"


@pytest.fixture(autouse=True)
def clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def api():
    return APIClient()


@pytest.fixture
def books(db):
    ledger.ensure_accounts()


def _login(phone=PHONE) -> tuple[Customer, str]:
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        accounts.request_code(phone)
    customer, raw, _ = accounts.verify_code(phone, CODE)
    return customer, raw


def _as(api: APIClient, raw: str) -> APIClient:
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return api


def _intent(reference="PP", url="https://pay.example/abc"):
    return PaymentIntent(transaction_id="PAY-1", reference=reference, redirect_url=url)


TRANSFER = {
    "source_wallet": "moncash",
    "destination_wallet": "natcash",
    "recipient_phone": "3212-3456",
    "net_amount": "1000",
    "expected_total": "1090.00",
}


def _create(api, key="cle-transfert-0001", body=None, intent=None, side_effect=None):
    with mock.patch(GET_CLIENT) as get_client:
        create = get_client.return_value.create_payment
        if side_effect is not None:
            create.side_effect = side_effect
        else:
            create.return_value = intent or _intent()
        response = api.post(reverse("api:transfers"), body or TRANSFER, format="json", HTTP_IDEMPOTENCY_KEY=key)
    return response, create


# ----------------------------------------------------------------------
# Numeros
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw", ["+509 3712-3456", "50937123456", "3712 3456", "00509.37.12.34.56", "(509) 3712-3456"]
)
def test_haitian_mobile_numbers_are_normalized(raw):
    assert normalize(raw) == PHONE


@pytest.mark.parametrize("raw", ["2812 3456", "5093712345", "+33612345678", "", "abcdefgh"])
def test_invalid_or_landline_numbers_are_refused(raw):
    with pytest.raises(InvalidPhone):
        normalize(raw)


# ----------------------------------------------------------------------
# Identification
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_otp_flow_issues_a_hashed_token(api):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        response = api.post(reverse("api:otp_request"), {"phone": "+509 3712 3456"}, format="json")
    assert response.status_code == 202
    assert response.json()["phone"] == PHONE
    assert not Customer.objects.exists()  # rien n'est cree avant validation du code

    response = api.post(reverse("api:otp_verify"), {"phone": PHONE, "code": CODE}, format="json")

    assert response.status_code == 200
    raw = response.json()["token"]
    token = CustomerToken.objects.get()
    assert raw.startswith("ppk_") and raw not in token.key_hash
    assert token.customer.phone == PHONE
    assert _as(api, raw).get(reverse("api:me")).json()["phone"] == PHONE


@pytest.mark.django_db
def test_wrong_codes_are_limited_then_the_code_is_burned(api):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        api.post(reverse("api:otp_request"), {"phone": PHONE}, format="json")

    statuses = [
        api.post(reverse("api:otp_verify"), {"phone": PHONE, "code": "000000"}, format="json").status_code
        for _ in range(5)
    ]
    assert statuses == [400] * 5
    assert api.post(reverse("api:otp_verify"), {"phone": PHONE, "code": "000000"}, format="json").status_code == 429
    # Le bon code ne passe plus : il a ete detruit.
    response = api.post(reverse("api:otp_verify"), {"phone": PHONE, "code": CODE}, format="json")
    assert response.json()["error"]["code"] == "OTP_INVALID"
    assert not CustomerToken.objects.exists()


@pytest.mark.django_db
def test_sms_requests_are_throttled_per_phone(api):
    first = api.post(reverse("api:otp_request"), {"phone": PHONE}, format="json")
    second = api.post(reverse("api:otp_request"), {"phone": "3712-3456"}, format="json")
    other = api.post(reverse("api:otp_request"), {"phone": OTHER_PHONE}, format="json")

    assert (first.status_code, second.status_code, other.status_code) == (202, 429, 202)
    assert second.json()["error"]["code"] == "RATE_LIMITED"


@pytest.mark.django_db
def test_disabled_customer_gets_no_sms_and_no_token(api):
    Customer.objects.create(phone=PHONE, is_active=False)
    with mock.patch("apps.accounts.otp.ConsoleOTPBackend.send") as send:
        response = api.post(reverse("api:otp_request"), {"phone": PHONE}, format="json")
    assert response.status_code == 202  # ne revele pas l'etat du compte
    send.assert_not_called()


@pytest.mark.django_db
def test_expired_revoked_and_disabled_tokens_are_refused(api):
    customer, raw = _login()
    me = reverse("api:me")
    assert _as(api, raw).get(me).status_code == 200

    assert _as(api, raw).post(reverse("api:logout")).status_code == 204
    assert _as(api, raw).get(me).status_code == 401

    _, raw = _login()
    CustomerToken.objects.filter(revoked_at__isnull=True).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _as(api, raw).get(me).status_code == 401

    _, raw = _login()
    Customer.objects.filter(pk=customer.pk).update(is_active=False)
    assert _as(api, raw).get(me).status_code == 401


@pytest.mark.django_db
def test_customer_token_does_not_open_the_console_and_staff_session_does_not_open_the_api(client, api):
    _, raw = _login()
    response = client.get(reverse("console:dashboard"), HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert response.status_code == 302 and "/login/" in response["Location"]

    staff = User.objects.create_user(username="sa", password="x", role=Role.SUPERADMIN)
    client.force_login(staff)
    response = client.get(reverse("api:transfers"))
    assert response.status_code == 401


# ----------------------------------------------------------------------
# Twilio Verify
# ----------------------------------------------------------------------
@pytest.fixture
def twilio(settings):
    settings.OTP = {**settings.OTP, "BACKEND": "twilio"}
    settings.TWILIO = {
        "ACCOUNT_SID": "AC_test",
        "AUTH_TOKEN": "secret",
        "VERIFY_SERVICE_SID": TWILIO_SERVICE,
        "TIMEOUT": 5,
    }
    base = f"https://verify.twilio.com/v2/Services/{TWILIO_SERVICE}"
    with responses.RequestsMock() as rsps:
        yield rsps, base


@pytest.mark.django_db
def test_twilio_verify_sends_and_checks_in_e164(api, twilio):
    rsps, base = twilio
    send = rsps.post(f"{base}/Verifications", json={"status": "pending"}, status=201)
    check = rsps.post(f"{base}/VerificationCheck", json={"status": "approved"}, status=200)

    assert api.post(reverse("api:otp_request"), {"phone": PHONE}, format="json").status_code == 202
    response = api.post(reverse("api:otp_verify"), {"phone": PHONE, "code": "424242"}, format="json")

    assert response.status_code == 200
    assert send.calls[0].request.body == "To=%2B50937123456&Channel=sms"
    assert check.calls[0].request.body == "To=%2B50937123456&Code=424242"
    assert send.calls[0].request.headers["Authorization"].startswith("Basic ")


@pytest.mark.django_db
def test_twilio_expired_code_and_outage_are_reported_distinctly(api, twilio):
    rsps, base = twilio
    rsps.post(f"{base}/VerificationCheck", json={"code": 20404, "message": "not found"}, status=404)
    rsps.post(f"{base}/Verifications", json={"message": "boom"}, status=503)

    expired = api.post(reverse("api:otp_verify"), {"phone": PHONE, "code": "424242"}, format="json")
    outage = api.post(reverse("api:otp_request"), {"phone": PHONE}, format="json")

    assert (expired.status_code, expired.json()["error"]["code"]) == (400, "OTP_INVALID")
    assert (outage.status_code, outage.json()["error"]["code"]) == (503, "OTP_UNAVAILABLE")


def test_console_otp_backend_is_refused_by_deploy_checks(settings):
    from apps.accounts.checks import otp_backend_is_production_ready

    settings.OTP = {**settings.OTP, "BACKEND": "console"}
    assert [e.id for e in otp_backend_is_production_ready(None)] == ["plipplip.E001"]
    settings.OTP = {**settings.OTP, "BACKEND": "twilio"}
    settings.TWILIO = {**settings.TWILIO, "AUTH_TOKEN": ""}
    # Twilio incomplet avertit sans bloquer le deploiement.
    assert [e.id for e in otp_backend_is_production_ready(None)] == ["plipplip.W002"]


# ----------------------------------------------------------------------
# Catalogue et devis
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_meta_lists_wallets_as_seeded(api):
    wallets = {w["code"]: w for w in api.get(reverse("api:meta")).json()["wallets"]}
    assert (wallets["moncash"]["payment_enabled"], wallets["moncash"]["payout_enabled"]) == (True, True)
    assert (wallets["kashpaw"]["payment_enabled"], wallets["kashpaw"]["payout_enabled"]) == (True, False)
    assert (wallets["carte"]["payment_enabled"], wallets["carte"]["payout_enabled"]) == (True, False)


@pytest.mark.django_db
def test_quote_matches_the_concept_note_example(api):
    response = api.post(
        reverse("api:quotes"),
        {"source_wallet": "carte", "destination_wallet": "natcash", "net_amount": "1000"},
        format="json",
    )
    assert response.status_code == 200
    body = response.json()
    assert (body["net_amount"], body["total_fees"], body["total_charged"]) == ("1000.00", "90.00", "1090.00")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body, code",
    [
        ({"source_wallet": "moncash", "destination_wallet": "kashpaw", "net_amount": "1000"}, "ROUTE_UNSUPPORTED"),
        ({"source_wallet": "moncash", "destination_wallet": "moncash", "net_amount": "1000"}, "ROUTE_UNSUPPORTED"),
        ({"source_wallet": "moncash", "destination_wallet": "natcash", "net_amount": "5"}, "AMOUNT_TOO_SMALL"),
        ({"source_wallet": "moncash", "destination_wallet": "natcash", "net_amount": "999999"}, "AMOUNT_TOO_LARGE"),
    ],
)
def test_quote_errors_carry_a_stable_code(api, body, code):
    response = api.post(reverse("api:quotes"), body, format="json")
    assert (response.status_code, response.json()["error"]["code"]) == (400, code)


# ----------------------------------------------------------------------
# Methodes : reglage superadmin
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_closed_method_is_refused_for_quotes_and_transfers(api):
    services.set_wallet_availability("kashpaw", direction="payment", enabled=False)
    body = {"source_wallet": "kashpaw", "destination_wallet": "natcash", "net_amount": "1000"}

    quote = api.post(reverse("api:quotes"), body, format="json")
    assert quote.json()["error"]["code"] == "METHOD_DISABLED"

    _, raw = _login()
    response, create = _create(_as(api, raw), body={**TRANSFER, **body})
    assert response.json()["error"]["code"] == "METHOD_DISABLED"
    create.assert_not_called()
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_closing_a_payout_wallet_never_blocks_money_already_collected(books):
    ledger.record_float_topup(Decimal("10000"), reference="RCH-1")
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH, recipient_phone=RECIPIENT,
        net_amount=Decimal("1000"),
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()

    services.set_wallet_availability("natcash", direction="payout", enabled=False)

    result = WithdrawalResult(
        transaction_id="WD-1", api_reference="1", reference=txn.build_payout_reference(), amount=txn.net_amount,
        fee=Decimal("25"), total=Decimal("1025"), balance_after=None, status="success",
    )
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.return_value = result
        services.execute_payout(txn)
    txn.refresh_from_db()
    assert txn.state == State.COMPLETED
    with pytest.raises(services.MethodDisabled):
        services.create_transaction(
            source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH, recipient_phone=RECIPIENT,
            net_amount=Decimal("1000"),
        )


@pytest.mark.django_db
def test_payout_cannot_be_opened_for_a_wallet_plopplop_cannot_pay_to():
    with pytest.raises(services.UnsupportedRoute):
        services.set_wallet_availability("carte", direction="payout", enabled=True)


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.OPERATOR, Role.APPROVER, Role.SUPPORT, Role.AUDITOR])
def test_only_superadmin_can_toggle_methods(client, role):
    user = User.objects.create_user(username=f"u-{role}", password="x", role=role)
    client.force_login(user)

    page = client.get(reverse("console:methods"))
    response = client.post(
        reverse("console:wallet_availability", args=["moncash"]), {"direction": "payment", "enabled": "0"},
        HTTP_HX_REQUEST="true",
    )

    assert page.status_code == 200 and b"data-console-action" not in page.content
    assert response.status_code == 403
    assert services.wallet_availability()[0]["payment_enabled"] is True
    log = AuditLog.objects.get(action="wallet.availability")
    assert (log.allowed, log.target, log.detail) == (False, "moncash", {"direction": "payment", "enabled": "0"})


@pytest.mark.django_db
def test_superadmin_toggles_a_method_and_it_is_audited(client):
    admin = User.objects.create_user(username="sa", password="x", role=Role.SUPERADMIN)
    client.force_login(admin)
    assert b"data-console-action" in client.get(reverse("console:methods")).content

    response = client.post(
        reverse("console:wallet_availability", args=["carte"]), {"direction": "payment", "enabled": "0"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200 and b'id="methods-body"' in response.content
    carte = next(r for r in services.wallet_availability() if r["wallet"] == "carte")
    assert (carte["payment_enabled"], carte["updated_by"]) == (False, admin)
    assert AuditLog.objects.get(action="wallet.availability").allowed

    refused = client.post(
        reverse("console:wallet_availability", args=["carte"]), {"direction": "payout", "enabled": "1"},
        HTTP_HX_REQUEST="true",
    )
    assert refused.status_code == 200
    assert AuditLog.objects.filter(action="wallet.availability", detail__outcome="error").count() == 1


# ----------------------------------------------------------------------
# Transferts
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_transfer_is_created_linked_to_customer_and_payment_started(api):
    customer, raw = _login()

    response, create = _create(_as(api, raw))

    assert response.status_code == 201
    body = response.json()
    txn = Transaction.objects.get(reference=body["reference"])
    assert (txn.customer, txn.state, txn.recipient_phone) == (customer, State.AWAITING_PAYMENT, RECIPIENT)
    assert body["status"] == "awaiting_payment"
    assert body["payment"] == {
        "mode": "redirect",
        "redirect_url": "https://pay.example/abc",
        "expires_at": body["payment"]["expires_at"],
    }
    create.assert_called_once()
    assert create.call_args.kwargs["amount"] == Decimal("1090.00")


@pytest.mark.django_db
def test_replayed_idempotency_key_returns_the_same_transfer_without_calling_plopplop_again(api):
    _, raw = _login()
    _as(api, raw)

    first, create_first = _create(api)
    second, create_second = _create(api)

    assert (first.status_code, second.status_code) == (201, 200)
    assert second["Idempotent-Replayed"] == "true"
    assert first.json()["reference"] == second.json()["reference"]
    create_first.assert_called_once()
    create_second.assert_not_called()
    assert Transaction.objects.count() == 1


@pytest.mark.django_db
def test_idempotency_key_reused_for_a_different_transfer_is_refused(api):
    _, raw = _login()
    _as(api, raw)
    _create(api)

    response, create = _create(api, body={**TRANSFER, "net_amount": "500", "expected_total": "545.00"})

    assert (response.status_code, response.json()["error"]["code"]) == (422, "IDEMPOTENCY_KEY_REUSED")
    create.assert_not_called()
    assert Transaction.objects.count() == 1


@pytest.mark.django_db
def test_idempotency_keys_are_scoped_per_customer(api):
    _, raw_a = _login(PHONE)
    _, raw_b = _login(OTHER_PHONE)

    a, _ = _create(_as(APIClient(), raw_a))
    b, _ = _create(_as(APIClient(), raw_b))

    assert (a.status_code, b.status_code) == (201, 201)
    assert a.json()["reference"] != b.json()["reference"]


@pytest.mark.django_db
@pytest.mark.parametrize("key", [None, "court", "espace interdit!"])
def test_transfer_requires_a_valid_idempotency_key(api, key):
    _, raw = _login()
    _as(api, raw)
    headers = {} if key is None else {"HTTP_IDEMPOTENCY_KEY": key}
    with mock.patch(GET_CLIENT) as get_client:
        response = api.post(reverse("api:transfers"), TRANSFER, format="json", **headers)
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    get_client.assert_not_called()


@pytest.mark.django_db
def test_stale_expected_total_is_refused_with_the_current_quote(api):
    _, raw = _login()

    response, create = _create(_as(api, raw), body={**TRANSFER, "expected_total": "1080.00"})

    assert response.status_code == 409
    error = response.json()["error"]
    assert (error["code"], error["quote"]["total_charged"]) == ("QUOTE_CHANGED", "1090.00")
    create.assert_not_called()
    assert not Transaction.objects.exists()


@pytest.mark.django_db
def test_indeterminate_payment_creation_is_never_retried_and_is_still_detected_by_polling(api, books):
    _, raw = _login()
    _as(api, raw)

    response, create = _create(api, side_effect=pp.PlopPlopIndeterminate("timeout"))

    assert response.status_code == 201
    assert response.json()["payment"]["mode"] == "unavailable"
    txn = Transaction.objects.get()
    assert txn.state == State.AWAITING_PAYMENT
    create.assert_called_once()

    replay, create_again = _create(api, side_effect=AssertionError("ne doit pas etre appele"))
    assert replay.status_code == 200
    create_again.assert_not_called()

    # Le client a finalement paye : paiement-verify interroge par reference.
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.payment_status.return_value = PaymentStatus(
            reference=txn.reference, transaction_id="PAY-9", confirmed=True, amount=txn.total_charged, method="moncash"
        )
        services.poll_payment(txn)
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_QUEUED
    assert api.get(reverse("api:transfer_detail", args=[txn.reference])).json()["status"] == "in_progress"


@pytest.mark.django_db
def test_explicit_payment_refusal_cancels_the_transfer(api):
    _, raw = _login()

    response, _ = _create(
        _as(api, raw), side_effect=pp.MethodNotConfigured("inactif", code="METHOD_NOT_CONFIGURED", status=400)
    )

    assert response.status_code == 502
    error = response.json()["error"]
    assert (error["code"], error["transfer"]["status"]) == ("PAYMENT_PROVIDER_ERROR", "cancelled")
    txn = Transaction.objects.get()
    assert (txn.state, txn.failure_code) == (State.CANCELLED, "METHOD_NOT_CONFIGURED")


@pytest.mark.django_db
def test_sender_phone_on_moncash_uses_ussd(api):
    _, raw = _login()

    response, create = _create(
        _as(api, raw), body={**TRANSFER, "sender_phone": "3712-3456"}, intent=_intent(url=None)
    )

    assert create.call_args.kwargs["method"] == "moncash_ussd"
    assert create.call_args.kwargs["phone_number"] == PHONE
    assert response.json()["payment"]["mode"] == "ussd"


@pytest.mark.django_db
def test_customer_never_sees_another_customers_transfers(api):
    _, raw_a = _login(PHONE)
    _, raw_b = _login(OTHER_PHONE)
    created, _ = _create(_as(APIClient(), raw_a))
    reference = created.json()["reference"]

    other = _as(APIClient(), raw_b)
    detail = other.get(reverse("api:transfer_detail", args=[reference]))
    listing = other.get(reverse("api:transfers"))

    assert (detail.status_code, detail.json()["error"]["code"]) == (404, "NOT_FOUND")
    assert listing.json()["results"] == []
    assert len(_as(APIClient(), raw_a).get(reverse("api:transfers")).json()["results"]) == 1


@pytest.mark.django_db
def test_internal_payout_trouble_is_shown_as_in_progress(api, books):
    _, raw = _login()
    _as(api, raw)
    created, _ = _create(api)
    txn = Transaction.objects.get(reference=created.json()["reference"])
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()

    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.side_effect = pp.PlopPlopIndeterminate("timeout")
        services.execute_payout(txn)
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_UNKNOWN

    body = api.get(reverse("api:transfer_detail", args=[txn.reference])).json()
    assert (body["status"], body["payment"]["mode"]) == ("in_progress", "none")
    assert "payout_unknown" not in str(body) and "failure" not in str(body)


def test_every_internal_state_has_a_public_status():
    from apps.api.status import MAPPING

    assert set(MAPPING) == set(State)


@pytest.mark.django_db
def test_unauthenticated_transfer_calls_get_401(api):
    assert api.get(reverse("api:transfers")).status_code == 401
    assert api.post(reverse("api:transfers"), TRANSFER, format="json").status_code == 401


# ----------------------------------------------------------------------
# Contrat et garde-fous structurels
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_openapi_schema_generates_without_warnings(tmp_path):
    call_command("spectacular", "--file", str(tmp_path / "schema.yml"), "--validate", "--fail-on-warn")
    assert "/api/v1/transfers" in (tmp_path / "schema.yml").read_text(encoding="utf-8")


def test_only_providers_talk_to_twilio_or_plopplop_over_http():
    offenders = [
        path.relative_to(APPS_DIR).as_posix()
        for path in APPS_DIR.rglob("*.py")
        if "providers" not in path.parts
        and any(marker in path.read_text(encoding="utf-8") for marker in ("import requests", "twilio.com", "solutionip.app"))
    ]
    assert offenders == []


def test_api_views_do_not_import_models_for_writing():
    source = (APPS_DIR / "api" / "views.py").read_text(encoding="utf-8")
    for forbidden in (".save(", ".create(", ".update(", ".delete(", "transition("):
        assert forbidden not in source, forbidden
