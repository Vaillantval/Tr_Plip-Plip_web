"""Avis par WhatsApp, au choix du client, avec repli SMS.

Regles protegees : WhatsApp seulement si le client l'a choisi ET que le
canal est pret ; repli SMS sur un echec confirme par Twilio, une seule
fois, jamais en plus d'un WhatsApp livre ; aucun statut accepte sans
signature Twilio valide ; l'accord du client est date.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from decimal import Decimal
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import Customer, NotificationChannel
from apps.ledger import services as ledger
from apps.notifications import services as notifications
from apps.notifications import tasks as notification_tasks
from apps.notifications import whatsapp
from apps.notifications.models import Channel, Notification, Status, Template
from apps.providers.plopplop.client import PaymentIntent
from apps.providers.twilio import exceptions as tw
from apps.providers.twilio.signature import expected_signature
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

PHONE = "50937123456"
RECIPIENT = "50932123456"
CALLBACK = "https://plip.example/webhooks/twilio/status/"
GET_CLIENT = "apps.transactions.services.get_client"
SEND = "apps.providers.twilio.sms.TwilioSMSClient.send"
SEND_WA = "apps.providers.twilio.sms.TwilioSMSClient.send_whatsapp"
SENT = mock.Mock(sid="SM123", status="queued")
SENT_WA = mock.Mock(sid="SMWA1", status="queued")
WHATSAPP = {
    "WHATSAPP_FROM": "+14155550100",
    "STATUS_CALLBACK_URL": CALLBACK,
    "WHATSAPP_CONTENT_SIDS": {
        "transfer_completed": "HX1",
        "transfer_refunded": "HX2",
        "claim_answered": "HX3",
    },
}


@pytest.fixture(autouse=True)
def books(db, settings):
    ledger.ensure_accounts()
    settings.TWILIO = {
        **settings.TWILIO,
        "ACCOUNT_SID": "AC1",
        "AUTH_TOKEN": "token",
        "MESSAGING_SERVICE_SID": "MG1",
        "FROM_NUMBER": "",
        **WHATSAPP,
    }
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def deliver_inline():
    """Envoi synchrone : les tests n'ont pas de courtier Celery."""
    with mock.patch(
        "apps.notifications.services._publish",
        side_effect=lambda pk: notification_tasks.send_notification(pk),
    ):
        yield


@pytest.fixture
def customer(db):
    return Customer.objects.create(phone=PHONE, language="ht", notification_channel=NotificationChannel.WHATSAPP)


@contextmanager
def sending(capture, wa=None, sms=None):
    """Patcher AVANT la capture : voir test_notifications.sending()."""
    with mock.patch(SEND_WA, **(wa or {"return_value": SENT_WA})) as send_wa, mock.patch(
        SEND, **(sms or {"return_value": SENT})
    ) as send_sms, capture(execute=True):
        yield send_wa, send_sms


def _queued(customer, amount="1000"):
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone=RECIPIENT,
        net_amount=Decimal(amount),
        customer=customer,
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    return Transaction.objects.get(pk=txn.pk)


def _settle(txn):
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.return_value = mock.Mock(
            succeeded=True, fee=Decimal("40"), transaction_id="PP-1", api_reference="", balance_after=None
        )
        services.execute_payout(txn)
    return Transaction.objects.get(pk=txn.pk)


def _post_status(client, note, *, status="undelivered", error="63003", sid=None, sign=True, token="token"):
    params = {"MessageSid": sid or note.provider_message_id or "SMWA1", "MessageStatus": status, "ErrorCode": error}
    url = f"{CALLBACK}?n={note.pk}"
    headers = {"HTTP_X_TWILIO_SIGNATURE": expected_signature(url, params, token)} if sign else {}
    return client.post(f"{reverse('twilio_status')}?n={note.pk}", params, **headers)


# ----------------------------------------------------------------------
# Le canal choisi
# ----------------------------------------------------------------------
def test_a_customer_who_chose_whatsapp_gets_the_approved_template(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks) as (send_wa, send_sms):
        _settle(txn)

    note = Notification.objects.get(transaction=txn)
    assert (note.channel, note.status, note.provider_message_id) == (Channel.WHATSAPP, Status.SENT, "SMWA1")
    kwargs = send_wa.call_args.kwargs
    assert kwargs["to"] == f"+{PHONE}" and kwargs["content_sid"] == "HX1"
    assert kwargs["variables"] == {"1": "1 000,00", "2": RECIPIENT, "3": txn.reference}
    assert kwargs["status_callback"] == f"{CALLBACK}?n={note.pk}"
    send_sms.assert_not_called()


def test_the_journal_keeps_the_text_the_customer_reads(customer, django_capture_on_commit_callbacks):
    """En francais et accentue : c'est le modele approuve, pas le SMS."""
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks):
        _settle(txn)

    note = Notification.objects.get(transaction=txn)
    assert note.language == "fr"
    assert note.body == f"Plip-Plip : 1 000,00 HTG ont été envoyés au {RECIPIENT}. Référence : {txn.reference}. Merci de votre confiance."


def test_whatsapp_not_ready_means_sms_even_if_chosen(customer, settings, django_capture_on_commit_callbacks):
    settings.TWILIO = {**settings.TWILIO, "WHATSAPP_CONTENT_SIDS": {**WHATSAPP["WHATSAPP_CONTENT_SIDS"], "claim_answered": ""}}
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks) as (send_wa, send_sms):
        _settle(txn)

    assert Notification.objects.get(transaction=txn).channel == Channel.SMS
    send_wa.assert_not_called()
    assert send_sms.call_count == 1


def test_a_customer_who_kept_sms_never_gets_whatsapp(customer, django_capture_on_commit_callbacks):
    Customer.objects.filter(pk=customer.pk).update(notification_channel=NotificationChannel.SMS)
    txn = _queued(Customer.objects.get(pk=customer.pk))
    with sending(django_capture_on_commit_callbacks) as (send_wa, send_sms):
        _settle(txn)

    send_wa.assert_not_called()
    assert send_sms.call_count == 1


# ----------------------------------------------------------------------
# Repli SMS
# ----------------------------------------------------------------------
def test_an_undelivered_whatsapp_falls_back_to_one_sms(customer, client, django_capture_on_commit_callbacks):
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks):
        _settle(txn)
    wa = Notification.objects.get(transaction=txn, channel=Channel.WHATSAPP)

    with mock.patch(SEND, return_value=SENT) as send_sms, django_capture_on_commit_callbacks(execute=True):
        first = _post_status(client, wa)
        again = _post_status(client, wa)  # Twilio peut rappeler

    assert first.status_code == again.status_code == 204
    wa.refresh_from_db()
    assert (wa.status, wa.error_code) == (Status.FAILED, "WA_63003")
    sms = Notification.objects.get(transaction=txn, channel=Channel.SMS)
    assert sms.status == Status.SENT and sms.language == "ht"
    assert send_sms.call_count == 1, "un seul SMS de repli, meme si Twilio rappelle"


@pytest.mark.parametrize("status", ["queued", "sent", "delivered", "read"])
def test_a_delivered_whatsapp_never_triggers_an_sms(customer, client, status, django_capture_on_commit_callbacks):
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks):
        _settle(txn)
    wa = Notification.objects.get(transaction=txn)

    with mock.patch(SEND) as send_sms:
        assert _post_status(client, wa, status=status, error="").status_code == 204

    send_sms.assert_not_called()
    assert not Notification.objects.filter(transaction=txn, channel=Channel.SMS).exists()


def test_a_refused_whatsapp_falls_back_at_once(customer, django_capture_on_commit_callbacks):
    """Refus immediat de Twilio (expediteur mal configure, numero refuse)."""
    txn = _queued(customer)
    refused = {"side_effect": tw.TwilioError("Invalid From", code=63007, status=400)}
    with sending(django_capture_on_commit_callbacks, wa=refused) as (_, send_sms):
        _settle(txn)

    assert Notification.objects.get(transaction=txn, channel=Channel.WHATSAPP).status == Status.FAILED
    assert Notification.objects.get(transaction=txn, channel=Channel.SMS).status == Status.SENT
    assert send_sms.call_count == 1


def test_a_failure_reported_during_the_send_call_is_not_overwritten(customer, django_capture_on_commit_callbacks):
    """Le statut d'echec peut arriver avant que nous ayons ecrit « envoye »."""
    txn = _queued(customer)

    def failed_before_we_write(**kwargs):
        note_id = int(kwargs["status_callback"].split("?n=")[1])
        notifications.record_whatsapp_status(
            notification_id=note_id, message_sid="SMWA1", message_status="failed", error_code="63003"
        )
        return SENT_WA

    with sending(django_capture_on_commit_callbacks, wa={"side_effect": failed_before_we_write}) as (_, send_sms):
        _settle(txn)

    assert Notification.objects.get(transaction=txn, channel=Channel.WHATSAPP).status == Status.FAILED
    assert send_sms.call_count == 1


def test_a_status_for_another_message_is_ignored(customer, client, django_capture_on_commit_callbacks):
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks):
        _settle(txn)
    wa = Notification.objects.get(transaction=txn)

    with mock.patch(SEND) as send_sms:
        _post_status(client, wa, sid="SMAUTRE")

    send_sms.assert_not_called()
    wa.refresh_from_db()
    assert wa.status == Status.SENT


# ----------------------------------------------------------------------
# La signature
# ----------------------------------------------------------------------
@pytest.mark.parametrize("forgery", ["unsigned", "wrong_token"])
def test_an_unsigned_status_is_refused_and_changes_nothing(customer, client, forgery, django_capture_on_commit_callbacks):
    """Sans ce controle, n'importe qui declencherait un SMS paye par nous."""
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks):
        _settle(txn)
    wa = Notification.objects.get(transaction=txn)

    with mock.patch(SEND) as send_sms:
        if forgery == "unsigned":
            response = _post_status(client, wa, sign=False)
        else:
            response = _post_status(client, wa, token="devine")

    assert response.status_code == 403
    send_sms.assert_not_called()
    wa.refresh_from_db()
    assert wa.status == Status.SENT


# ----------------------------------------------------------------------
# Les modeles
# ----------------------------------------------------------------------
@pytest.mark.parametrize("template", list(Template))
def test_every_template_is_one_meta_will_accept(template, customer):
    """Meta refuse un modele qui commence ou finit par une variable."""
    text = whatsapp.TEMPLATES[template]
    assert not text.startswith("{{") and not re.search(r"\}\}\W*$", text)

    txn = _queued(customer)
    placeholders = set(re.findall(r"\{\{(\d+)\}\}", text))
    assert placeholders == set(whatsapp.variables(template, txn)), "chaque variable a sa place, et inversement"
    assert "{{" not in whatsapp.render_body(template, txn)


# ----------------------------------------------------------------------
# Le choix, sur la page de confirmation
# ----------------------------------------------------------------------
SEND_FORM = {
    "source_wallet": "moncash",
    "destination_wallet": "natcash",
    "recipient_phone": "3212 3456",
    "net_amount": "1000",
    "sender_phone": "",
}


def _logged_in(client, customer):
    session = client.session
    session["web:customer_id"] = customer.pk
    session["web:last_seen"] = 9999999999
    session.save()
    client.post(reverse("web:send"), SEND_FORM)
    return client.get(reverse("web:confirm")).content.decode()


def _confirm(client, page, **extra):
    hidden = lambda name: re.search(rf'name="{name}" value="([^"]+)"', page).group(1)  # noqa: E731
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.create_payment.return_value = PaymentIntent(
            transaction_id="PAY-1", reference="PP", redirect_url="https://pay.example/abc"
        )
        return client.post(
            reverse("web:confirm"),
            {
                "idempotency_key": hidden("idempotency_key"),
                "expected_total": hidden("expected_total"),
                "recipient_phone_confirm": RECIPIENT,
                **extra,
            },
        )


def test_choosing_whatsapp_records_the_consent_and_its_time(client):
    customer = Customer.objects.create(phone=PHONE)
    page = _logged_in(client, customer)
    assert 'value="whatsapp"' in page

    assert _confirm(client, page, notify_by="whatsapp").status_code == 302

    customer.refresh_from_db()
    assert customer.notification_channel == NotificationChannel.WHATSAPP
    assert customer.whatsapp_opt_in_at is not None


def test_going_back_to_sms_withdraws_the_consent(client, customer):
    Customer.objects.filter(pk=customer.pk).update(whatsapp_opt_in_at="2026-10-01T00:00:00Z")
    page = _logged_in(client, customer)
    assert re.search(r'value="whatsapp"[^>]*checked', page), "le choix enregistre est preselectionne"

    _confirm(client, page, notify_by="sms")

    customer.refresh_from_db()
    assert (customer.notification_channel, customer.whatsapp_opt_in_at) == (NotificationChannel.SMS, None)


def test_whatsapp_is_not_offered_until_it_is_ready(client, settings):
    settings.TWILIO = {**settings.TWILIO, "WHATSAPP_FROM": ""}
    customer = Customer.objects.create(phone=PHONE)
    page = _logged_in(client, customer)
    assert 'value="whatsapp"' not in page

    _confirm(client, page, notify_by="whatsapp")  # force a la main

    customer.refresh_from_db()
    assert customer.notification_channel == NotificationChannel.SMS, "un choix que la page n'a pas montre n'est pas un accord"
    assert customer.whatsapp_opt_in_at is None
