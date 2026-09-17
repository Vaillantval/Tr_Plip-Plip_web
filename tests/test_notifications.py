"""Notifications SMS : transfert livre, transfert rembourse.

Regles protegees : un seul SMS par transaction et par evenement, meme si
la tache est rejouee ou le processus tue au mauvais moment ; rien au
beneficiaire ; un echec SMS ne touche jamais une transaction ; sans
identifiants Twilio, aucun appel reseau et le deploiement passe.
"""

from __future__ import annotations

import re
import subprocess
import sys
from datetime import timedelta
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
import responses
from django.core.cache import cache
from django.db import IntegrityError, transaction as db_transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services as accounts
from apps.accounts.models import Customer
from apps.ledger import services as ledger
from apps.notifications import services as notifications
from apps.notifications import tasks as notification_tasks
from apps.notifications.models import Channel, Notification, Status, Template
from apps.providers.twilio import exceptions as tw
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

ROOT = Path(__file__).resolve().parent.parent
CODE = "123456"
PHONE = "50937123456"
RECIPIENT = "50932123456"
GET_CLIENT = "apps.transactions.services.get_client"
SEND = "apps.providers.twilio.sms.TwilioSMSClient.send"
SENT = mock.Mock(sid="SM123", status="queued")
#: Capture AVANT toute fixture : mock.patch remplace l'attribut du module,
#: pas l'objet fonction. C'est le vrai _publish, celui qui parle au courtier.
REAL_PUBLISH = notifications._publish


@pytest.fixture(autouse=True)
def books(db, settings):
    ledger.ensure_accounts()
    settings.TWILIO = {
        **settings.TWILIO,
        "ACCOUNT_SID": "AC1",
        "AUTH_TOKEN": "token",
        "MESSAGING_SERVICE_SID": "MG1",
        "FROM_NUMBER": "",
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
    return Customer.objects.create(phone=PHONE, language="fr")


@contextmanager
def sending(capture, **patch):
    """Patch l'envoi AVANT la capture, jamais l'inverse.

    django_capture_on_commit_callbacks execute les rappels a la SORTIE de
    son contexte : imbrique a l'interieur du patch, il s'executerait une
    fois le patch retire -- et le test partirait vraiment chez Twilio.
    """
    with mock.patch(SEND, **patch) as send, capture(execute=True):
        yield send


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


# ----------------------------------------------------------------------
# Le SMS part, une seule fois, au bon destinataire
# ----------------------------------------------------------------------
def test_a_delivered_transfer_sends_one_sms_to_the_sender(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)

    with sending(django_capture_on_commit_callbacks, return_value=SENT) as send:
        _settle(txn)

    note = Notification.objects.get(transaction=txn, template=Template.TRANSFER_COMPLETED)
    assert (note.status, note.recipient, note.provider_message_id) == (Status.SENT, PHONE, "SM123")
    assert txn.reference in note.body and "HTG" in note.body
    assert send.call_count == 1
    assert send.call_args.kwargs["to"] == f"+{PHONE}"


def test_no_sms_ever_goes_to_the_beneficiary(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)

    with sending(django_capture_on_commit_callbacks, return_value=SENT) as send:
        _settle(txn)

    assert send.call_args.kwargs["to"] != f"+{RECIPIENT}"
    assert Notification.objects.filter(recipient=RECIPIENT).count() == 0


def test_a_refund_sends_its_own_sms(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)

    with sending(django_capture_on_commit_callbacks, return_value=SENT):
        services.refund(txn, reason="Erreur operateur", transfer_reference="MANUEL-1")

    note = Notification.objects.get(transaction=txn, template=Template.TRANSFER_REFUNDED)
    assert note.status == Status.SENT
    assert "rembourse" in note.body


def test_a_transfer_without_customer_sends_nothing():
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH,
        recipient_phone=RECIPIENT, net_amount=Decimal("1000"),
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)

    with mock.patch(SEND) as send:
        _settle(Transaction.objects.get(pk=txn.pk))

    send.assert_not_called()
    assert Notification.objects.get(transaction=txn).status == Status.SKIPPED


# ----------------------------------------------------------------------
# Idempotence
# ----------------------------------------------------------------------
def test_replaying_the_task_never_sends_a_second_sms(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks, return_value=SENT) as send:
        _settle(txn)
    note = Notification.objects.get(transaction=txn)

    with mock.patch(SEND, return_value=SENT) as again:
        assert notification_tasks.send_notification(note.pk) == "skipped"

    assert send.call_count == 1
    again.assert_not_called()


def test_the_database_refuses_a_second_notification_row(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks, return_value=SENT):
        _settle(txn)

    with pytest.raises(IntegrityError):
        Notification.objects.create(
            transaction=txn, template=Template.TRANSFER_COMPLETED, channel=Channel.SMS, recipient=PHONE
        )


def test_twilio_is_told_the_same_key_so_a_crash_cannot_double_the_sms(customer, django_capture_on_commit_callbacks):
    """Le trou : tue entre la reponse de Twilio et l'ecriture du statut."""
    txn = _queued(customer)

    with sending(django_capture_on_commit_callbacks, return_value=SENT) as send:
        _settle(txn)
    note = Notification.objects.get(transaction=txn)
    first_key = send.call_args.kwargs["idempotency_key"]

    # Le processus est tue avant l'ecriture : la ligne reste « a envoyer ».
    Notification.objects.filter(pk=note.pk).update(status=Status.PENDING, provider_message_id="")
    with mock.patch(SEND, return_value=SENT) as retry:
        notification_tasks.send_notification(note.pk)

    assert retry.call_args.kwargs["idempotency_key"] == first_key  # Twilio dedoublonne
    assert first_key.endswith(f"{txn.pk}-{Template.TRANSFER_COMPLETED}-{Channel.SMS}")


def test_nothing_is_enqueued_if_the_settlement_rolls_back(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(RuntimeError), db_transaction.atomic():
            with mock.patch("apps.ledger.services.record_payout_executed", side_effect=RuntimeError("grand livre")):
                _settle(txn)

    assert callbacks == []
    assert not Notification.objects.exists()
    assert Transaction.objects.get(pk=txn.pk).state != State.COMPLETED


# ----------------------------------------------------------------------
# Un echec SMS ne touche jamais une transaction
# ----------------------------------------------------------------------
def test_an_unreachable_broker_never_fails_the_payout(customer, django_capture_on_commit_callbacks, monkeypatch):
    """Redis injoignable au moment de mettre le SMS en file.

    Ce code tourne dans un rappel on_commit, donc APRES que la base a
    valide le decaissement. Une exception qui remonte affiche une erreur a
    l'operateur alors que l'argent est deja parti -- et il relance un
    decaissement deja fait. C'est la pire categorie de defaut sur ce
    systeme : l'ecran ment dans le sens qui coute de l'argent.
    """
    # La fixture autouse remplace _publish par un envoi sur place : on
    # remet le vrai, c'est lui que ce test interroge.
    monkeypatch.setattr(notifications, "_publish", REAL_PUBLISH)
    txn = _queued(customer)

    with mock.patch(
        "apps.notifications.tasks.send_notification.apply_async", side_effect=OSError("Redis injoignable")
    ), django_capture_on_commit_callbacks(execute=True):
        settled = _settle(txn)

    assert settled.state == State.COMPLETED
    # La ligne reste a envoyer : c'est le balayage periodique qui la reprend.
    note = Notification.objects.get(transaction=txn, template=Template.TRANSFER_COMPLETED)
    assert note.status == Status.PENDING
    assert note.attempts == 0


def test_a_notification_failure_never_fails_the_transfer(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)

    with mock.patch(
        "apps.notifications.services.render_body", side_effect=RuntimeError("modele casse")
    ), django_capture_on_commit_callbacks(execute=True):
        settled = _settle(txn)

    assert settled.state == State.COMPLETED
    assert settled.journal_entries.filter(reference__endswith="-PAYOUT").exists()


def test_an_invalid_phone_is_a_permanent_failure(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)

    with sending(
        django_capture_on_commit_callbacks,
        side_effect=tw.TwilioInvalidPhone("numero invalide", code=21211, status=400),
    ) as send:
        _settle(txn)

    note = Notification.objects.get(transaction=txn)
    assert (note.status, note.attempts) == (Status.FAILED, 1)
    assert note.error_code == "21211"
    assert send.call_count == 1  # aucune reprise


def test_a_transient_failure_is_retried_then_gives_up(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks, return_value=SENT):
        _settle(txn)
    note = Notification.objects.get(transaction=txn)

    Notification.objects.filter(pk=note.pk).update(status=Status.PENDING, attempts=0)
    unavailable = tw.TwilioUnavailable("twilio injoignable")
    with mock.patch(SEND, side_effect=unavailable), mock.patch.object(
        notification_tasks.send_notification, "retry", side_effect=Exception("reprise")
    ) as retry:
        with pytest.raises(Exception, match="reprise"):
            notification_tasks.send_notification(note.pk)

    retry.assert_called_once()
    note.refresh_from_db()
    assert note.status == Status.PENDING  # rendue a la file


def test_the_notification_is_abandoned_once_too_old(customer, django_capture_on_commit_callbacks, settings):
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks, return_value=SENT):
        _settle(txn)
    note = Notification.objects.get(transaction=txn)

    old = timezone.now() - timedelta(seconds=settings.NOTIFICATIONS["MAX_AGE_SECONDS"] + 60)
    Notification.objects.filter(pk=note.pk).update(status=Status.PENDING, created_at=old)
    with mock.patch(SEND) as send:
        assert notification_tasks.send_notification(note.pk) == "skipped"

    send.assert_not_called()
    note.refresh_from_db()
    assert (note.status, note.error_code) == (Status.SKIPPED, "TOO_LATE")


# ----------------------------------------------------------------------
# Sans Twilio
# ----------------------------------------------------------------------
@responses.activate
def test_missing_credentials_block_nothing_and_call_nothing(customer, django_capture_on_commit_callbacks, settings):
    settings.TWILIO = {**settings.TWILIO, "MESSAGING_SERVICE_SID": "", "FROM_NUMBER": ""}
    txn = _queued(customer)

    with django_capture_on_commit_callbacks(execute=True):
        settled = _settle(txn)

    assert settled.state == State.COMPLETED
    note = Notification.objects.get(transaction=txn)
    assert (note.status, note.error_code) == (Status.SKIPPED, "SMS_NOT_CONFIGURED")
    assert [c.request.url for c in responses.calls] == []  # aucune requete reseau


def test_deploy_checks_only_warn_about_the_missing_sender():
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "TWILIO_", "PLOPPLOP_", "DATABASE_"))}
    env.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            "DJANGO_SECRET_KEY": "k3y-" + "x9" * 30,
            "PLOPPLOP_CLIENT_ID": "id",
            "PLOPPLOP_CLIENT_SECRET": "secret",
            "DJANGO_ALLOWED_HOSTS": "plip.ht",
        }
    )
    result = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "ERROR"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "plipplip.W003" in result.stdout + result.stderr


# ----------------------------------------------------------------------
# Langue du client
# ----------------------------------------------------------------------
@pytest.mark.parametrize("language, marker", [("ht", "voye bay"), ("en", "sent to"), ("fr", "ont ete envoyes")])
def test_the_sms_is_written_in_the_customers_language(
    customer, django_capture_on_commit_callbacks, language, marker
):
    Customer.objects.filter(pk=customer.pk).update(language=language)
    txn = _queued(Customer.objects.get(pk=customer.pk))

    with sending(django_capture_on_commit_callbacks, return_value=SENT):
        _settle(txn)

    note = Notification.objects.get(transaction=txn)
    assert (note.language, marker in note.body) == (language, True)


def test_an_unknown_language_falls_back_to_french(django_capture_on_commit_callbacks):
    customer = Customer.objects.create(phone=PHONE, language="")
    txn = _queued(customer)

    with sending(django_capture_on_commit_callbacks, return_value=SENT):
        _settle(txn)

    assert Notification.objects.get(transaction=txn).language == "fr"


def test_the_language_is_captured_at_login_on_both_paths(client):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:login"), {"phone": PHONE})
    client.cookies["django_language"] = "ht"
    client.post(reverse("web:login_code"), {"code": CODE})
    assert Customer.objects.get(phone=PHONE).language == "ht"

    from rest_framework.test import APIClient

    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        accounts.request_code(PHONE)
    APIClient().post(
        reverse("api:otp_verify"), {"phone": PHONE, "code": CODE, "language": "en"}, format="json"
    )
    assert Customer.objects.get(phone=PHONE).language == "en"


# ----------------------------------------------------------------------
# Journal et reprise
# ----------------------------------------------------------------------
def test_every_send_is_logged(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)

    with sending(django_capture_on_commit_callbacks, return_value=SENT):
        _settle(txn)

    note = Notification.objects.get(transaction=txn)
    for field in ("recipient", "template", "status", "provider_message_id", "body", "language"):
        assert getattr(note, field), field
    assert note.sent_at and note.attempts == 1


def test_a_notification_left_behind_is_picked_up_again(customer, django_capture_on_commit_callbacks):
    txn = _queued(customer)
    with sending(django_capture_on_commit_callbacks, return_value=SENT):
        _settle(txn)
    note = Notification.objects.get(transaction=txn)
    # Processus tue entre le commit et la publication de la tache.
    Notification.objects.filter(pk=note.pk).update(
        status=Status.PENDING, attempts=0, created_at=timezone.now() - timedelta(minutes=10)
    )

    with mock.patch.object(notification_tasks.send_notification, "apply_async") as publish:
        assert notification_tasks.sweep_pending() == {"republished": 1}

    publish.assert_called_once()
    assert publish.call_args.args[0] == (note.pk,)


# ----------------------------------------------------------------------
# Garde-fou des traductions
# ----------------------------------------------------------------------
LOCALE_DIR = ROOT / "apps" / "notifications" / "locale"


def _catalog(language):
    """Entrees actives du catalogue SMS."""
    import ast

    text = (LOCALE_DIR / language / "LC_MESSAGES" / "django.po").read_text(encoding="utf-8")
    entries = []
    for block in re.split(r"\n\s*\n", text):
        parts, current, obsolete, flags = {"msgid": [], "msgstr": []}, None, False, []
        for line in block.splitlines():
            if line.startswith("#~"):
                obsolete = True
            elif line.startswith("#,"):
                flags += [f.strip() for f in line[2:].split(",")]
            elif line.startswith(("msgid ", "msgstr ")):
                current, _, rest = line.partition(" ")
                parts[current].append(ast.literal_eval(rest))
            elif line.startswith('"') and current:
                parts[current].append(ast.literal_eval(line))
        msgid = "".join(parts["msgid"])
        if msgid and not obsolete:
            entries.append({"msgid": msgid, "msgstr": "".join(parts["msgstr"]), "flags": flags})
    return entries


@pytest.mark.parametrize("language", ["en", "ht"])
def test_every_sms_string_is_translated_and_compiled(language):
    po = LOCALE_DIR / language / "LC_MESSAGES" / "django.po"
    entries = _catalog(language)

    assert len(entries) == len(Template.choices)
    assert [e["msgid"] for e in entries if not e["msgstr"]] == []
    assert [e["msgid"] for e in entries if "fuzzy" in e["flags"]] == []
    mo = po.with_suffix(".mo")
    assert mo.exists() and mo.stat().st_mtime >= po.stat().st_mtime - 1


def test_no_sms_string_can_ever_wait_for_its_creole():
    """Un SMS parle toujours d'argent : aucune derogation possible."""
    assert not (LOCALE_DIR / "ht" / "A_TRADUIRE.md").exists()


@pytest.mark.parametrize("language", ["fr", "en", "ht"])
def test_an_sms_never_costs_two_segments(language, customer):
    """Un accent ferait tomber le message a 70 caracteres par segment."""
    from django.utils import translation as django_translation

    from apps.notifications import messages as sms_messages

    txn = mock.Mock(
        net_amount=Decimal("50000"), recipient_phone=RECIPIENT, reference="PP-2609-4MCEU7K9"
    )
    for template, render in sms_messages.RENDERERS.items():
        with django_translation.override(language):
            body = render(txn)
        assert len(body) <= 160, (language, template, len(body))
        assert all(ord(c) < 128 or c in "£¥èéùìòÇØøÅåÆæßÉÄÖÑÜ§¿äöñüà" for c in body), (language, template, body)
