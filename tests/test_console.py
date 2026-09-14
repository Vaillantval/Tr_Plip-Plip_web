"""Tests de la console d'exploitation.

Comme test_core : chaque test protege une regle d'exploitation. Les
roles sans droit d'action sont SUPPORT et AUDITOR ; APPROVER n'agit pas
non plus sur l'argent (ACTING_ROLES) et est teste avec eux.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AuditLog, Role, User
from apps.ledger import services as ledger
from apps.ledger.models import JournalEntry, LedgerAccount
from apps.providers.plopplop import exceptions as pp
from apps.providers.plopplop.client import WithdrawalResult, WithdrawalStatus
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State
from apps.treasury import services as treasury
from apps.treasury.models import FloatAlert, FloatSnapshot

APPS_DIR = Path(__file__).resolve().parent.parent / "apps"
GET_CLIENT = "apps.transactions.services.get_client"
HTMX = {"HTTP_HX_REQUEST": "true"}
NON_ACTING_ROLES = [Role.SUPPORT, Role.AUDITOR, Role.APPROVER]
ACTION_MARKER = b"data-console-action"


@pytest.fixture
def books(db):
    ledger.ensure_accounts()


@pytest.fixture
def operator(db):
    return User.objects.create_user(username="operateur", password="x", role=Role.OPERATOR)


def _user(role) -> User:
    return User.objects.create_user(username=f"user-{role}", password="x", role=role)


# ----------------------------------------------------------------------
# Mise en situation : toujours par les services, jamais par les modeles
# ----------------------------------------------------------------------
def _queued_txn(net_amount=Decimal("1000")) -> Transaction:
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone="50912345678",
        sender_phone="50937000000",
        net_amount=net_amount,
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()
    return txn


def _failed_txn() -> Transaction:
    txn = _queued_txn()
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.side_effect = pp.InsufficientBalance(
            "Solde epuise", code="INSUFFICIENT_BALANCE", status=400
        )
        services.execute_payout(txn)
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_FAILED
    return txn


def _unknown_txn() -> Transaction:
    txn = _queued_txn()
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.side_effect = pp.PlopPlopIndeterminate("timeout")
        services.execute_payout(txn)
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_UNKNOWN
    return txn


def _verify(txn: Transaction, *, status: str | None = None, error: Exception | None = None):
    with mock.patch(GET_CLIENT) as get_client:
        client = get_client.return_value
        if error is not None:
            client.withdrawal_status.side_effect = error
        else:
            client.withdrawal_status.return_value = WithdrawalStatus(
                reference=txn.payout_reference,
                status=status,
                transaction_id="API_WD_NA_1" if status == "success" else None,
                amount=None,
            )
        services.verify_unknown_payout(txn)
    txn.refresh_from_db()
    return client


def _pending_txn() -> Transaction:
    txn = _unknown_txn()
    _verify(txn, status="pending")
    assert txn.state == State.PAYOUT_PENDING
    return txn


def _settle(txn: Transaction, *, fee=Decimal("25"), balance_after=None) -> None:
    result = WithdrawalResult(
        transaction_id="API_WD_NA_1",
        api_reference="9876543210",
        reference=txn.build_payout_reference(),
        amount=txn.net_amount,
        fee=fee,
        total=txn.net_amount + fee,
        balance_after=balance_after,
        status="success",
    )
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.return_value = result
        services.execute_payout(txn)
    txn.refresh_from_db()
    assert txn.state == State.COMPLETED


def _alert() -> FloatAlert:
    return FloatAlert.objects.create(
        level=FloatAlert.Level.CRITICAL, balance=Decimal("0"), message="Float critique : 0 HTG"
    )


# ----------------------------------------------------------------------
# Roles
# ----------------------------------------------------------------------
@pytest.mark.django_db
@pytest.mark.parametrize("role", NON_ACTING_ROLES)
def test_non_acting_roles_are_refused_and_every_refusal_is_audited(client, books, role):
    txn = _failed_txn()
    alert = _alert()
    user = _user(role)
    client.force_login(user)
    events_before = txn.events.count()
    entries_before = JournalEntry.objects.count()

    attempts = [
        ("payout.retry", reverse("console:payout_retry", args=[txn.reference]), {}),
        ("payout.verify", reverse("console:payout_verify", args=[txn.reference]), {}),
        (
            "transaction.refund",
            reverse("console:transaction_refund", args=[txn.reference]),
            {"reason": "Client injoignable", "transfer_reference": "MC-1"},
        ),
        ("float.alert_ack", reverse("console:alert_acknowledge", args=[alert.pk]), {}),
        ("float.topup", reverse("console:float_topup"), {"amount": "5000", "reference": "RCH-1"}),
    ]
    with mock.patch(GET_CLIENT) as get_client:
        for action, url, data in attempts:
            response = client.post(url, data, **HTMX)
            assert response.status_code == 403, action
            log = AuditLog.objects.get(action=action)
            assert (log.allowed, log.user) == (False, user), action
    get_client.assert_not_called()

    # Le refus garde la trace de ce qui a ete tente.
    assert AuditLog.objects.get(action="transaction.refund").detail["transfer_reference"] == "MC-1"
    assert not AuditLog.objects.filter(allowed=True).exists()

    txn.refresh_from_db()
    alert.refresh_from_db()
    assert txn.state == State.PAYOUT_FAILED
    assert txn.events.count() == events_before
    assert JournalEntry.objects.count() == entries_before
    assert alert.acknowledged_at is None


def _pages_with_actions() -> list[str]:
    failed = _failed_txn()
    unknown = _unknown_txn()
    _alert()
    return [
        reverse("console:transaction_detail", args=[failed.reference]),
        reverse("console:transaction_detail", args=[unknown.reference]),
        reverse("console:exceptions"),
        reverse("console:treasury"),
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("role", NON_ACTING_ROLES)
def test_non_acting_roles_see_no_action_button(client, books, role):
    pages = _pages_with_actions()
    client.force_login(_user(role))
    for url in pages:
        response = client.get(url)
        assert response.status_code == 200, url
        assert ACTION_MARKER not in response.content, url


@pytest.mark.django_db
def test_acting_role_sees_action_buttons(client, books, operator):
    # Contre-epreuve : sans elle, le test precedent passerait sur un
    # template qui n'affiche aucun bouton a personne.
    pages = _pages_with_actions()
    client.force_login(operator)
    for url in pages:
        assert ACTION_MARKER in client.get(url).content, url


# ----------------------------------------------------------------------
# Journal d'audit
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_accepted_action_writes_one_authorized_audit_row(client, books, operator):
    txn = _failed_txn()
    client.force_login(operator)

    response = client.post(
        reverse("console:payout_retry", args=[txn.reference]), **HTMX, HTTP_HX_TARGET="exceptions-body"
    )

    assert response.status_code == 200
    assert b'id="exceptions-body"' in response.content
    assert b"<nav" not in response.content  # fragment, pas la page
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_QUEUED
    assert txn.events.last().actor == operator

    log = AuditLog.objects.get(action="payout.retry")
    assert (log.allowed, log.target, log.user) == (True, txn.reference, operator)
    assert "outcome" not in log.detail


@pytest.mark.django_db
def test_authorized_action_that_fails_writes_a_second_audit_row(client, books, operator):
    # Issue inconnue : la relance doit etre refusee par le service.
    txn = _unknown_txn()
    client.force_login(operator)

    client.post(reverse("console:payout_retry", args=[txn.reference]), **HTMX)

    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_UNKNOWN
    logs = list(AuditLog.objects.filter(action="payout.retry").order_by("id"))
    assert [log.allowed for log in logs] == [True, True]
    assert "outcome" not in logs[0].detail
    assert logs[1].detail["outcome"] == "error"


# ----------------------------------------------------------------------
# Remboursement
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_refund_balances_the_ledger(client, books, operator):
    txn = _failed_txn()
    client.force_login(operator)

    response = client.post(
        reverse("console:transaction_refund", args=[txn.reference]),
        {"reason": "Beneficiaire injoignable", "transfer_reference": "MC-778899"},
        **HTMX,
    )

    assert response.status_code == 200
    txn.refresh_from_db()
    assert txn.state == State.REFUNDED

    assert all(entry.is_balanced() for entry in JournalEntry.objects.all())
    for code in (ledger.CLIENTS_PAYABLE, ledger.REVENUE_COMMISSION):
        assert LedgerAccount.objects.get(code=code).balance() == Decimal("0"), code
    # L'encaissement reste sur le float ; le remboursement manuel sort de la tresorerie.
    assert LedgerAccount.objects.get(code=ledger.FLOAT).balance() == txn.total_charged
    assert LedgerAccount.objects.get(code=ledger.CASH_SETTLEMENT).balance() == -txn.total_charged

    entry = JournalEntry.objects.get(reference=f"{txn.reference}-REFUND")
    assert "Beneficiaire injoignable" in entry.description
    assert "MC-778899" in entry.description
    assert entry.posted_by == operator

    log = AuditLog.objects.get(action="transaction.refund")
    assert log.allowed
    assert log.detail == {"reason": "Beneficiaire injoignable", "transfer_reference": "MC-778899"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "data",
    [
        {"reason": "Client injoignable", "transfer_reference": ""},
        {"reason": "", "transfer_reference": "MC-1"},
        {"reason": "   ", "transfer_reference": "MC-1"},
    ],
)
def test_refund_without_reason_or_transfer_reference_is_not_recorded(client, books, operator, data):
    txn = _failed_txn()
    client.force_login(operator)

    response = client.post(reverse("console:transaction_refund", args=[txn.reference]), data, **HTMX)

    assert response.status_code == 200
    assert "NON enregistre" in response.content.decode()
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_FAILED
    assert not JournalEntry.objects.filter(reference=f"{txn.reference}-REFUND").exists()
    assert AuditLog.objects.filter(action="transaction.refund", detail__outcome="error").count() == 1


@pytest.mark.django_db
def test_refund_service_refuses_blank_transfer_reference(books):
    txn = _failed_txn()
    with pytest.raises(services.InvalidRefund):
        services.refund(txn, reason="Client injoignable", transfer_reference="   ")
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_FAILED


# ----------------------------------------------------------------------
# Decaissements indetermines et en attente
# ----------------------------------------------------------------------
def _not_found():
    return pp.PlopPlopError("Introuvable", status=404)


def _at(moment):
    return mock.patch("django.utils.timezone.now", return_value=moment)


@pytest.mark.django_db
def test_first_not_found_on_unknown_payout_never_requeues(client, books, operator):
    # Juste apres un timeout, un 404 peut n'etre qu'une course
    # ecriture/lecture chez plopplop.
    txn = _unknown_txn()
    client.force_login(operator)

    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdrawal_status.side_effect = _not_found()
        client.post(reverse("console:payout_verify", args=[txn.reference]), **HTMX)

    get_client.return_value.withdrawal_status.assert_called_once()
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_UNKNOWN
    observation = txn.events.last()
    assert (observation.from_state, observation.to_state, observation.actor) == (
        State.PAYOUT_UNKNOWN,
        State.PAYOUT_UNKNOWN,
        operator,
    )

    response = client.get(reverse("console:exceptions"))
    unknown = next(g for g in response.context["groups"] if g["state"] == State.PAYOUT_UNKNOWN)
    assert [row["txn"].pk for row in unknown["rows"]] == [txn.pk]


@pytest.mark.django_db
def test_second_not_found_within_grace_period_does_not_requeue(books, settings):
    settings.PAYOUT_VERIFY_GRACE_SECONDS = 600
    now = timezone.now()
    with _at(now - timedelta(minutes=20)):
        txn = _unknown_txn()
    with _at(now - timedelta(minutes=5)):
        _verify(txn, error=_not_found())

    _verify(txn, error=_not_found())

    assert txn.state == State.PAYOUT_UNKNOWN
    assert txn.events.filter(from_state=State.PAYOUT_UNKNOWN, to_state=State.PAYOUT_UNKNOWN).count() == 2


@pytest.mark.django_db
def test_unknown_payout_is_requeued_after_two_not_found_spaced_by_grace_period(client, books, operator, settings):
    settings.PAYOUT_VERIFY_GRACE_SECONDS = 600
    now = timezone.now()
    with _at(now - timedelta(minutes=30)):
        txn = _unknown_txn()
    with _at(now - timedelta(minutes=15)):
        _verify(txn, error=_not_found())  # premier 404, par la tache planifiee
    assert txn.state == State.PAYOUT_UNKNOWN
    client.force_login(operator)

    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdrawal_status.side_effect = _not_found()
        client.post(reverse("console:payout_verify", args=[txn.reference]), **HTMX)

    get_client.return_value.withdrawal_status.assert_called_once()
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_QUEUED
    entered = txn.events.get(from_state=State.PAYOUT_IN_FLIGHT, to_state=State.PAYOUT_UNKNOWN)
    after = list(txn.events.filter(id__gt=entered.id).values_list("from_state", "to_state", "actor"))
    assert after == [
        (State.PAYOUT_UNKNOWN, State.PAYOUT_UNKNOWN, None),
        (State.PAYOUT_UNKNOWN, State.PAYOUT_FAILED, operator.pk),
        (State.PAYOUT_FAILED, State.PAYOUT_QUEUED, operator.pk),
    ]


@pytest.mark.django_db
def test_verification_observation_does_not_reset_time_in_state(client, books):
    with _at(timezone.now() - timedelta(hours=3)):
        _unknown_txn()
    txn = Transaction.objects.get(state=State.PAYOUT_UNKNOWN)
    _verify(txn, error=_not_found())
    client.force_login(_user(Role.SUPPORT))

    response = client.get(reverse("console:exceptions"))

    unknown = next(g for g in response.context["groups"] if g["state"] == State.PAYOUT_UNKNOWN)
    assert unknown["rows"][0]["since_seconds"] >= 3 * 3600


@pytest.mark.django_db
def test_still_pending_payout_writes_nothing(books):
    txn = _pending_txn()
    events_before = txn.events.count()

    client = _verify(txn, status="pending")

    client.withdrawal_status.assert_called_once()
    assert txn.state == State.PAYOUT_PENDING
    assert txn.events.count() == events_before


@pytest.mark.django_db
def test_pending_payout_is_resolved_by_verification(books):
    txn = _pending_txn()
    _verify(txn, status="success")
    assert txn.state == State.COMPLETED
    # Une verification ne renvoie pas balance_after : pas de releve.
    assert not FloatSnapshot.objects.exists()


@pytest.mark.django_db
def test_pending_payout_not_found_is_never_requeued(books):
    # L'operateur a reconnu le retrait : un 404 ulterieur ne prouve pas
    # que rien n'est parti.
    txn = _pending_txn()
    _verify(txn, error=pp.PlopPlopError("Introuvable", status=404))
    assert txn.state == State.PAYOUT_PENDING


@pytest.mark.django_db
def test_pending_stuck_beyond_threshold_comes_first_on_exceptions_screen(client, books, settings):
    settings.PAYOUT_PENDING_STALE_SECONDS = 2 * 3600
    recent = _pending_txn()
    with mock.patch("django.utils.timezone.now", return_value=timezone.now() - timedelta(hours=6)):
        stuck = _pending_txn()
    failed = _failed_txn()
    client.force_login(_user(Role.SUPPORT))

    response = client.get(reverse("console:exceptions"))

    assert [row["txn"].pk for row in response.context["stale"]] == [stuck.pk]
    pending = next(g for g in response.context["groups"] if g["state"] == State.PAYOUT_PENDING)
    assert [row["txn"].pk for row in pending["rows"]] == [recent.pk]
    html = response.content.decode()
    assert 'data-stale="true"' in html
    assert html.index(stuck.reference) < html.index(failed.reference) < html.index(recent.reference)


# ----------------------------------------------------------------------
# Tresorerie
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_displaying_the_float_never_creates_an_alert(client, books):
    _queued_txn()  # float de 1090 HTG, sous le seuil critique : alerte attendue
    client.force_login(_user(Role.SUPPORT))

    for name in ("console:dashboard", "console:treasury", "console:queue"):
        assert client.get(reverse(name)).status_code == 200, name
    assert not FloatAlert.objects.exists()

    treasury.evaluate_float()  # la tache planifiee, elle, alerte
    assert FloatAlert.objects.count() == 1


@pytest.mark.django_db
def test_burn_rate_counts_actual_payout_fees(books):
    ledger.record_float_topup(Decimal("10000"), reference="RCH-1")
    _settle(_queued_txn(), fee=Decimal("25"))
    assert treasury.burn_rate(24) == Decimal("1025")


@pytest.mark.django_db
def test_successful_payout_records_a_float_snapshot(books):
    ledger.record_float_topup(Decimal("10000"), reference="RCH-1")
    txn = _queued_txn()

    _settle(txn, fee=Decimal("25"), balance_after=Decimal("10060"))

    snapshot = FloatSnapshot.objects.get()
    assert snapshot.transaction_id == txn.pk
    assert snapshot.provider_balance == Decimal("10060")
    # 10000 recharges + 1090 encaisses - 1025 decaisses.
    assert snapshot.ledger_balance == Decimal("10065")
    assert snapshot.drift == Decimal("-5")


@pytest.mark.django_db
def test_queue_coverage_shows_the_rank_where_the_queue_stalls(books):
    _unknown_txn()  # 1000 net + 25 de retrait NatCash estime, engages avant la file
    queued = [_queued_txn(Decimal("2000")) for _ in range(5)]
    assert queued[0].provider_fee_out_estimate == Decimal("50")  # NatCash 2,5 %, tarif de depart
    # Les encaissements creditent le float (1090 + 5 x 2180 = 11990) : la file
    # se finance seule. Pour la faire caler, on simule une sortie de tresorerie.
    ledger.post(
        reference="SORTIE-1",
        description="Retrait de tresorerie",
        lines=[(ledger.FLOAT, Decimal("-1965"), ""), (ledger.CASH_SETTLEMENT, Decimal("1965"), "")],
    )

    coverage = treasury.queue_coverage()

    # 10025 - 1025 = 9000 disponibles ; 4 x 2050 = 8200 passent, pas 5 x 2050.
    assert coverage["available"] == Decimal("9000")
    assert coverage["depth"] == 5
    assert coverage["total_net"] == Decimal("10000")
    assert coverage["stall_rank"] == 5
    assert coverage["covered_count"] == 4


@pytest.mark.django_db
def test_queue_screen_orders_by_confirmation_and_shows_drain_time(client, books, settings):
    settings.PAYOUT_COOLDOWN_SECONDS = 125
    first, second, third = (_queued_txn() for _ in range(3))
    client.force_login(_user(Role.SUPPORT))

    response = client.get(reverse("console:queue"))

    assert response.context["drain_seconds"] == 375
    assert [row["txn"].pk for row in response.context["rows"]] == [first.pk, second.pk, third.pk]
    # Les encaissements de la file sont deja sur le float : elle passe entiere.
    assert response.context["coverage"]["stall_rank"] is None

    fragment = client.get(reverse("console:queue"), **HTMX)
    assert b'id="queue-body"' in fragment.content
    assert b"<nav" not in fragment.content


@pytest.mark.django_db
def test_topup_reference_can_only_be_recorded_once(client, books, operator):
    client.force_login(operator)
    url = reverse("console:float_topup")

    for _ in range(2):
        assert client.post(url, {"amount": "5000", "reference": "RCH-42"}, **HTMX).status_code == 200

    assert LedgerAccount.objects.get(code=ledger.FLOAT).balance() == Decimal("5000")
    logs = list(AuditLog.objects.filter(action="float.topup").order_by("id"))
    assert [(log.allowed, log.detail.get("outcome")) for log in logs] == [(True, None), (True, None), (True, "error")]
    assert logs[0].detail == {"amount": "5000", "reference": "RCH-42"}


@pytest.mark.django_db
@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-100")])
def test_topup_service_refuses_non_positive_amount(books, amount):
    with pytest.raises(ledger.InvalidTopup):
        ledger.record_float_topup(amount, reference="RCH-1")
    assert not JournalEntry.objects.exists()


@pytest.mark.django_db
def test_alert_is_acknowledged_once(client, books, operator):
    alert = _alert()
    client.force_login(operator)
    url = reverse("console:alert_acknowledge", args=[alert.pk])

    client.post(url, **HTMX)
    alert.refresh_from_db()
    first_acknowledgement = alert.acknowledged_at
    assert alert.acknowledged_by == operator
    assert first_acknowledgement is not None

    client.post(url, **HTMX)
    alert.refresh_from_db()
    assert alert.acknowledged_at == first_acknowledgement
    assert AuditLog.objects.filter(action="float.alert_ack", detail__outcome="error").count() == 1


# ----------------------------------------------------------------------
# Garde-fous structurels
# ----------------------------------------------------------------------
def _python_sources(directory: Path):
    for path in directory.rglob("*.py"):
        yield path, path.read_text(encoding="utf-8")


def test_state_is_only_assigned_inside_transactions_models():
    assignment = re.compile(r"\.state\s*=(?!=)")
    offenders = [
        f"{path.relative_to(APPS_DIR).as_posix()}:{lineno}"
        for path, source in _python_sources(APPS_DIR)
        for lineno, line in enumerate(source.splitlines(), start=1)
        if assignment.search(line)
    ]
    assert offenders
    assert all(o.startswith("transactions/models.py:") for o in offenders), offenders


def test_console_neither_calls_plopplop_nor_writes_float_alerts():
    offenders = [
        path.relative_to(APPS_DIR).as_posix()
        for path, source in _python_sources(APPS_DIR / "console")
        if "providers" in source or "evaluate_float" in source
    ]
    assert offenders == []
