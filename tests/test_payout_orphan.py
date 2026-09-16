"""Decaissements orphelins : « en cours » sans processus derriere.

Un worker tue pendant l'appel a plopplop laisse la transaction en
PAYOUT_IN_FLIGHT pour toujours. Regles protegees : le balayage les reprend
en PAYOUT_UNKNOWN (jamais vers la file), l'age est lu dans le journal
d'evenements, l'operateur les voit, et le lot de decaissement ne dort plus
-- c'est ce sommeil qui fabriquait les orphelins a chaque deploiement.
"""

from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.console import views as console_views
from apps.ledger import services as ledger
from apps.providers.plopplop.client import WithdrawalStatus
from apps.transactions import services, tasks
from apps.transactions.models import Transaction, TransactionEvent, Wallet
from apps.transactions.states import ALLOWED, State

GET_CLIENT = "apps.transactions.services.get_client"
EXECUTE_PAYOUT = "apps.transactions.services.execute_payout"
THRESHOLD = 600


@pytest.fixture(autouse=True)
def books(db, settings):
    ledger.ensure_accounts()
    settings.PAYOUT_INFLIGHT_STALE_SECONDS = THRESHOLD
    cache.clear()
    yield
    cache.clear()


def _confirmed() -> Transaction:
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone="50932123456",
        net_amount=Decimal("1000"),
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    return txn


def _in_flight(age_seconds: int | None = None) -> Transaction:
    """Decaissement en cours, entre dans l'etat il y a `age_seconds`."""
    txn = _confirmed()
    txn.payout_reference = txn.build_payout_reference()
    txn.payout_attempts = 1
    txn.save(update_fields=["payout_reference", "payout_attempts", "updated_at"])
    txn.transition(State.PAYOUT_IN_FLIGHT, note="Tentative 1")
    if age_seconds is not None:
        _age_entry(txn, age_seconds)
    return Transaction.objects.get(pk=txn.pk)


def _age_entry(txn, seconds: int) -> None:
    """Recule l'evenement d'entree dans l'etat courant."""
    event = txn.events.filter(to_state=txn.state).exclude(from_state=txn.state).latest("created_at", "id")
    TransactionEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(seconds=seconds))


def _paid(txn):
    return txn.transition(State.PAYOUT_IN_FLIGHT)


# ----------------------------------------------------------------------
# Balayage
# ----------------------------------------------------------------------
def test_in_flight_payout_becomes_unknown_after_the_threshold():
    orphan = _in_flight(age_seconds=THRESHOLD + 60)

    assert services.sweep_stale_in_flight_payouts() == {"checked": 1, "swept": 1}

    orphan.refresh_from_db()
    assert orphan.state == State.PAYOUT_UNKNOWN
    event = orphan.events.latest("created_at", "id")
    assert (event.from_state, event.to_state) == (State.PAYOUT_IN_FLIGHT, State.PAYOUT_UNKNOWN)
    assert event.data["payout_reference"] == orphan.payout_reference
    assert event.data["swept_after_seconds"] >= THRESHOLD


def test_a_recent_in_flight_payout_is_never_swept():
    fresh = _in_flight(age_seconds=THRESHOLD - 5)

    assert services.sweep_stale_in_flight_payouts() == {"checked": 0, "swept": 0}

    fresh.refresh_from_db()
    assert fresh.state == State.PAYOUT_IN_FLIGHT


def test_a_swept_payout_never_goes_back_to_the_queue():
    orphan = _in_flight(age_seconds=THRESHOLD + 60)

    services.sweep_stale_in_flight_payouts()

    orphan.refresh_from_db()
    # Ni le balayage ni la machine a etats n'offrent ce chemin.
    assert State.PAYOUT_QUEUED not in ALLOWED[State.PAYOUT_UNKNOWN]
    assert not orphan.events.filter(from_state=State.PAYOUT_UNKNOWN, to_state=State.PAYOUT_QUEUED).exists()
    assert not Transaction.objects.payable().exists()


def test_age_is_read_from_the_event_journal_not_updated_at():
    orphan = _in_flight(age_seconds=THRESHOLD + 60)
    # Une sauvegarde quelconque deplace updated_at : l'orphelin parait neuf.
    orphan.poll_count += 1
    orphan.save(update_fields=["poll_count", "updated_at"])

    assert services.sweep_stale_in_flight_payouts()["swept"] == 1


def test_a_swept_payout_is_then_resolved_by_verification():
    orphan = _in_flight(age_seconds=THRESHOLD + 60)
    services.sweep_stale_in_flight_payouts()

    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdrawal_status.return_value = WithdrawalStatus(
            reference=orphan.payout_reference,
            status="success",
            transaction_id="PP-OK",
            amount=orphan.net_amount,
        )
        assert tasks.resolve_unknown_payouts() == {"checked": 1, "resolved": 1}

    orphan.refresh_from_db()
    assert orphan.state == State.COMPLETED


def test_the_sweep_task_reports_what_it_did():
    _in_flight(age_seconds=THRESHOLD + 60)
    assert tasks.sweep_stale_payouts() == {"checked": 1, "swept": 1}


# ----------------------------------------------------------------------
# Console
# ----------------------------------------------------------------------
def test_orphan_appears_on_the_exceptions_screen(client):
    orphan = _in_flight(age_seconds=THRESHOLD + 60)
    fresh = _in_flight(age_seconds=10)
    client.force_login(User.objects.create_user(username="op", password="x", role=Role.OPERATOR))

    html = client.get(reverse("console:exceptions")).content.decode()
    context = console_views._exceptions_context()

    assert 'data-group="orphans"' in html
    assert fresh.reference in html  # visible aussi, mais hors de la section rouge
    assert [row["txn"].pk for row in context["orphans"]] == [orphan.pk]
    assert [row["txn"].pk for row in context["stale"]] == []


def test_a_healthy_platform_shows_no_orphan_section(client):
    _in_flight(age_seconds=10)
    client.force_login(User.objects.create_user(username="op2", password="x", role=Role.OPERATOR))

    assert 'data-group="orphans"' not in client.get(reverse("console:exceptions")).content.decode()


def test_every_stale_state_is_also_an_exception_state():
    # Sinon _exceptions_context leve KeyError sur grouped[txn.state].
    assert set(console_views.STALE_AFTER) <= set(console_views.EXCEPTION_STATES)


# ----------------------------------------------------------------------
# La cause : le lot ne dort plus
# ----------------------------------------------------------------------
def test_drain_hands_back_instead_of_waiting_for_the_cooldown(settings):
    settings.PAYOUT_COOLDOWN_SECONDS = 125
    _confirmed()
    _confirmed()
    cache.set(tasks.LAST_PAYOUT_KEY, time.time(), None)  # retrait tout juste effectue

    with mock.patch(EXECUTE_PAYOUT) as execute:
        assert tasks.drain_payout_queue() == {"processed": 0, "cooldown": True}

    execute.assert_not_called()
    assert Transaction.objects.payable().count() == 2


def test_drain_takes_one_payout_then_hands_back(settings):
    settings.PAYOUT_COOLDOWN_SECONDS = 125
    for _ in range(3):
        _confirmed()

    with mock.patch(EXECUTE_PAYOUT, side_effect=_paid):
        result = tasks.drain_payout_queue()

    # Le cooldown est arme des le premier retrait : la tache rend la main.
    assert result == {"processed": 1, "cooldown": True}
    assert Transaction.objects.payable().count() == 2


def test_no_task_sleeps_in_process():
    """Un processus qui dort finit tue au milieu d'un retrait."""
    assert not hasattr(tasks, "_sleep")
    _confirmed()
    never_sleep = mock.patch.object(time, "sleep", side_effect=AssertionError("aucune tache ne doit dormir"))

    with never_sleep, mock.patch(EXECUTE_PAYOUT, side_effect=_paid):
        tasks.drain_payout_queue()
        tasks.sweep_stale_payouts()


def test_drain_is_bounded_to_two_payouts_per_invocation(settings):
    settings.PAYOUT_COOLDOWN_SECONDS = 0  # sans cooldown, seul max_batch borne le lot
    for _ in range(5):
        _confirmed()

    with mock.patch(EXECUTE_PAYOUT, side_effect=_paid):
        assert tasks.drain_payout_queue() == {"processed": 2}

    assert Transaction.objects.payable().count() == 3
