"""Deux fournisseurs de messagerie, Twilio et Sent, choisis dans la console.

Regles protegees : Twilio par defaut ; la bascule est reservee au
superadmin, journalisee, et refusee vers un fournisseur non pret ; un code
deja envoye reste verifie par celui qui l'a envoye ; un avis garde le
fournisseur fixe a sa mise en file ; Sent n'est jamais appele sans cle,
et aucune valeur secrete n'apparait dans la console.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest import mock

import pytest
import responses
from django.core.cache import cache
from django.urls import reverse

from apps.accounts import otp
from apps.accounts.models import AuditLog, Customer, Role, User
from apps.ledger import services as ledger
from apps.notifications import routing
from apps.notifications import services as notifications
from apps.notifications import tasks as notification_tasks
from apps.notifications.models import MessagingSetting, Notification, Provider, Status, Template
from apps.providers.sent import exceptions as st
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

PHONE = "50937123456"
RECIPIENT = "50932123456"
SENT_URL = "https://api.sent.dm/v3/messages"
SENT_KEY = "11111111-2222-3333-4444-555555555555"
TWILIO_SERVICE = "VA_test"
NOTE_TEMPLATES = {
    "otp": "otp-template",
    "transfer_completed": "tpl-completed",
    "transfer_refunded": "tpl-refunded",
    "claim_answered": "tpl-claim",
}


@pytest.fixture(autouse=True)
def books(db, settings):
    ledger.ensure_accounts()
    settings.OTP = {**settings.OTP, "BACKEND": "live"}
    settings.TWILIO = {
        **settings.TWILIO,
        "ACCOUNT_SID": "AC1",
        "AUTH_TOKEN": "twilio-secret-token",
        "VERIFY_SERVICE_SID": TWILIO_SERVICE,
        "MESSAGING_SERVICE_SID": "MG1",
        "FROM_NUMBER": "",
    }
    settings.SENT = {**settings.SENT, "API_KEY": "", "SANDBOX": False, "TEMPLATES": dict(NOTE_TEMPLATES)}
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def sent_key(settings):
    settings.SENT = {**settings.SENT, "API_KEY": SENT_KEY}


@pytest.fixture
def deliver_inline():
    with mock.patch(
        "apps.notifications.services._publish",
        side_effect=lambda pk: notification_tasks.send_notification(pk),
    ):
        yield


def _choose(otp_provider=Provider.TWILIO, notification_provider=Provider.TWILIO):
    MessagingSetting.objects.update_or_create(
        pk=1, defaults={"otp_provider": otp_provider, "notification_provider": notification_provider}
    )
    MessagingSetting.clear_cache()


def _accepted(rsps, message_id="msg-1"):
    rsps.add(
        responses.POST,
        SENT_URL,
        json={"success": True, "data": {"messages": [{"id": message_id, "status": "QUEUED"}]}},
        status=202,
    )


# ----------------------------------------------------------------------
# Defaut
# ----------------------------------------------------------------------
def test_twilio_is_the_default_for_codes_and_notices():
    assert not MessagingSetting.objects.exists()
    assert (routing.otp_provider(), routing.notification_provider()) == ("twilio", "twilio")


def test_live_backend_without_a_row_sends_codes_through_twilio_verify():
    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.POST,
            f"https://verify.twilio.com/v2/Services/{TWILIO_SERVICE}/Verifications",
            json={"status": "pending"},
        )
        otp.get_backend().send(PHONE)
        assert len(rsps.calls) == 1


def test_legacy_twilio_backend_value_is_still_accepted(settings):
    settings.OTP = {**settings.OTP, "BACKEND": "twilio"}
    assert isinstance(otp.get_backend(), otp.LiveOTPBackend)


# ----------------------------------------------------------------------
# Codes par Sent
# ----------------------------------------------------------------------
def test_sent_code_is_sent_with_the_template_and_checked_locally(sent_key):
    _choose(otp_provider=Provider.SENT)
    with responses.RequestsMock() as rsps:
        _accepted(rsps)
        otp.get_backend().send(PHONE)
        request = rsps.calls[0].request
    body = json.loads(request.body)
    assert request.headers["x-api-key"] == SENT_KEY
    assert request.headers["Idempotency-Key"].startswith("plipplip-otp-")
    assert body["to"] == ["+50937123456"]
    assert body["template"]["id"] == NOTE_TEMPLATES["otp"]
    code = body["template"]["parameters"]["var_1"]
    assert code.isdigit() and len(code) == otp.CODE_LENGTH

    backend = otp.get_backend()
    assert backend.check(PHONE, "000000" if code != "000000" else "111111") is False
    assert backend.check(PHONE, code) is True
    assert backend.check(PHONE, code) is False  # usage unique


def test_sent_codes_are_throttled_per_number(sent_key):
    _choose(otp_provider=Provider.SENT)
    with responses.RequestsMock() as rsps:
        _accepted(rsps)
        otp.get_backend().send(PHONE)
        with pytest.raises(otp.OTPRateLimited):
            otp.get_backend().send(PHONE)
        assert len(rsps.calls) == 1


def test_sent_send_failure_leaves_no_valid_code(sent_key):
    _choose(otp_provider=Provider.SENT)
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, SENT_URL, json={"success": False, "error": {"code": "INTERNAL_001"}}, status=500)
        with pytest.raises(otp.OTPUnavailable):
            otp.get_backend().send(PHONE)
    assert cache.get(f"plipplip:otp:{PHONE}") is None


def test_sent_without_key_never_calls_the_network():
    _choose(otp_provider=Provider.SENT)
    # RequestsMock sans reponse enregistree : tout appel reseau leverait.
    with responses.RequestsMock(), pytest.raises(otp.OTPUnavailable):
        otp.get_backend().send(PHONE)


def test_a_twilio_code_is_still_checked_by_twilio_after_switching_to_sent(sent_key):
    base = f"https://verify.twilio.com/v2/Services/{TWILIO_SERVICE}"
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, f"{base}/Verifications", json={"status": "pending"})
        otp.get_backend().send(PHONE)

        _choose(otp_provider=Provider.SENT)
        rsps.add(responses.POST, f"{base}/VerificationCheck", json={"status": "approved"})
        assert otp.get_backend().check(PHONE, "123456") is True
        assert [c.request.url.rsplit("/", 1)[-1] for c in rsps.calls] == ["Verifications", "VerificationCheck"]


# ----------------------------------------------------------------------
# Avis par Sent
# ----------------------------------------------------------------------
@pytest.fixture
def customer(db):
    return Customer.objects.create(phone=PHONE, language="fr")


def _queued(customer):
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone=RECIPIENT,
        net_amount=Decimal("1000"),
        customer=customer,
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    return Transaction.objects.get(pk=txn.pk)


def _notify(txn, capture):
    with capture(execute=True):
        notifications.notify_transfer_completed(txn)
    return Notification.objects.get(transaction=txn, template=Template.TRANSFER_COMPLETED)


def test_a_notice_through_sent_is_sent_once_with_its_variables(
    customer, sent_key, deliver_inline, django_capture_on_commit_callbacks
):
    _choose(notification_provider=Provider.SENT)
    txn = _queued(customer)
    with responses.RequestsMock() as rsps:
        _accepted(rsps, "msg-42")
        note = _notify(txn, django_capture_on_commit_callbacks)
        request = rsps.calls[0].request

    body = json.loads(request.body)
    assert body["to"] == ["+50937123456"]
    assert body["template"]["id"] == "tpl-completed"
    assert body["template"]["parameters"]["var_3"] == txn.reference
    assert request.headers["Idempotency-Key"] == note.idempotency_key
    assert (note.provider, note.status, note.provider_message_id) == ("sent", Status.SENT, "msg-42")
    assert note.body.startswith("Plip-Plip :")


def test_a_sent_refusal_is_final_and_never_falls_back(
    customer, sent_key, deliver_inline, django_capture_on_commit_callbacks
):
    _choose(notification_provider=Provider.SENT)
    txn = _queued(customer)
    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.POST,
            SENT_URL,
            json={"success": False, "error": {"code": "VALIDATION_001", "details": {"template": ["x"]}}},
            status=400,
        )
        note = _notify(txn, django_capture_on_commit_callbacks)
    assert (note.status, note.error_code) == (Status.FAILED, "VALIDATION_001")
    assert Notification.objects.filter(transaction=txn).count() == 1


@pytest.mark.parametrize(
    "status, error",
    [(429, st.SentRateLimited), (503, st.SentUnavailable)],
)
def test_a_busy_sent_releases_the_notice_for_a_retry(customer, sent_key, status, error):
    _choose(notification_provider=Provider.SENT)
    txn = _queued(customer)
    with mock.patch("apps.notifications.services._publish"):
        notifications.notify_transfer_completed(txn)
    note = Notification.objects.get(transaction=txn)
    claimed = notifications.claim(note.pk)
    with responses.RequestsMock() as rsps:
        rsps.add(responses.POST, SENT_URL, json={"success": False}, status=status)
        with pytest.raises(error):
            notifications.deliver(claimed)
    assert Notification.objects.get(pk=note.pk).status == Status.PENDING


def test_the_provider_is_fixed_when_the_notice_is_queued(customer, sent_key):
    _choose(notification_provider=Provider.SENT)
    txn = _queued(customer)
    with mock.patch("apps.notifications.services._publish"):
        notifications.notify_transfer_completed(txn)
    _choose(notification_provider=Provider.TWILIO)

    note = notifications.claim(Notification.objects.get(transaction=txn).pk)
    with responses.RequestsMock() as rsps:
        _accepted(rsps)
        assert notifications.deliver(note) == Status.SENT
        assert rsps.calls[0].request.url == SENT_URL


# ----------------------------------------------------------------------
# Console
# ----------------------------------------------------------------------
def _user(role):
    return User.objects.create_user(username=f"user-{role}", password="x", role=role)


def _post(client, **data):
    payload = {"otp_provider": "twilio", "notification_provider": "twilio", **data}
    return client.post(reverse("console:messaging_update"), payload)


def test_only_the_superadmin_can_switch_and_the_refusal_is_audited(client):
    client.force_login(_user(Role.OPERATOR))
    assert _post(client, otp_provider="sent").status_code == 403
    assert AuditLog.objects.filter(action="messaging.update", allowed=False).exists()
    assert routing.otp_provider() == "twilio"


def test_switching_to_an_unready_sent_is_refused(client):
    client.force_login(_user(Role.SUPERADMIN))
    _post(client, otp_provider="sent")
    assert routing.otp_provider() == "twilio"
    assert AuditLog.objects.filter(action="messaging.update", detail__outcome="error").exists()


def test_switching_to_a_ready_sent_is_saved_and_audited(client, sent_key):
    admin = _user(Role.SUPERADMIN)
    client.force_login(admin)
    _post(client, otp_provider="sent", notification_provider="sent")
    assert (routing.otp_provider(), routing.notification_provider()) == ("sent", "sent")
    assert MessagingSetting.objects.get(pk=1).updated_by == admin
    assert AuditLog.objects.filter(action="messaging.update", allowed=True).exists()

    _post(client)  # retour sur Twilio, toujours permis
    assert (routing.otp_provider(), routing.notification_provider()) == ("twilio", "twilio")


def test_notices_cannot_switch_to_sent_without_their_templates(client, settings, sent_key):
    settings.SENT = {**settings.SENT, "TEMPLATES": {**NOTE_TEMPLATES, "claim_answered": ""}}
    client.force_login(_user(Role.SUPERADMIN))
    _post(client, otp_provider="sent", notification_provider="sent")
    assert routing.notification_provider() == "twilio"


def test_the_console_shows_missing_variable_names_never_values(client, sent_key, settings):
    settings.TWILIO = {**settings.TWILIO, "VERIFY_SERVICE_SID": ""}
    client.force_login(_user(Role.OPERATOR))
    html = client.get(reverse("console:methods")).content.decode()
    assert 'data-section="messaging"' in html
    assert "TWILIO_VERIFY_SERVICE_SID" in html
    assert SENT_KEY not in html and "twilio-secret-token" not in html
    assert 'name="otp_provider"' not in html  # lecture seule hors superadmin
