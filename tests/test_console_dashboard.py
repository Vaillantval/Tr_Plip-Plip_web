"""Tableau de bord de la console : chiffres, graphique, sante.

Regles protegees : les chiffres viennent de la base, sans appel a un
fournisseur ; la commission n'est montree qu'au superadmin ; un retrait
en echec plus recent que le dernier succes se voit.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Role, User
from apps.console import metrics
from apps.ledger import services as ledger
from apps.providers.plopplop import exceptions as pp
from apps.providers.plopplop.client import WithdrawalResult
from apps.transactions import services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

GET_CLIENT = "apps.transactions.services.get_client"


@pytest.fixture
def books(db):
    ledger.ensure_accounts()


def _user(role) -> User:
    return User.objects.create_user(username=f"user-{role}", password="x", role=role)


def _queued_txn(net_amount="1000", source=Wallet.MONCASH, destination=Wallet.NATCASH) -> Transaction:
    txn = services.create_transaction(
        source_wallet=source,
        destination_wallet=destination,
        recipient_phone="50932345678",
        sender_phone="50937000000",
        net_amount=Decimal(net_amount),
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()
    return txn


def _completed_txn(net_amount="1000", **route) -> Transaction:
    txn = _queued_txn(net_amount, **route)
    result = WithdrawalResult(
        transaction_id=f"API_WD_{txn.pk}",
        api_reference=f"98765{txn.pk}",
        reference=txn.build_payout_reference(),
        amount=txn.net_amount,
        fee=Decimal("25"),
        total=txn.net_amount + Decimal("25"),
        balance_after=None,
        status="success",
    )
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.return_value = result
        services.execute_payout(txn)
    txn.refresh_from_db()
    assert txn.state == State.COMPLETED
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


@pytest.mark.django_db
def test_delivered_counts_only_the_last_24_hours(books):
    recent = _completed_txn("1000")
    old = _completed_txn("5000")
    Transaction.objects.filter(pk=old.pk).update(payout_completed_at=timezone.now() - timedelta(hours=25))

    totals = metrics.delivered(24)
    assert totals["count"] == 1
    assert totals["volume"] == Decimal("1000")
    assert totals["platform_fees"] == recent.fee_platform


@pytest.mark.django_db
def test_volume_chart_scales_to_the_busiest_day_and_splits_by_route(books):
    _completed_txn("3000")
    _completed_txn("1000", source=Wallet.NATCASH, destination=Wallet.MONCASH)
    yesterday = _completed_txn("2000")
    Transaction.objects.filter(pk=yesterday.pk).update(payout_completed_at=timezone.now() - timedelta(days=1))

    chart = metrics.daily_volume(14)
    assert len(chart["bars"]) == 14
    today, before = chart["bars"][-1], chart["bars"][-2]
    assert today["total"] == Decimal("4000") and today["height"] == 100
    assert before["total"] == Decimal("2000") and before["height"] == 50
    assert sorted(s["share"] for s in today["segments"]) == [25, 75]
    # La route la plus chargee en premier dans la legende, couleur la plus foncee.
    assert chart["routes"][0]["label"] == "MonCash → NatCash"
    assert chart["routes"][0]["color"] == metrics.ROUTE_COLORS[0]


@pytest.mark.django_db
def test_in_flight_groups_follow_the_operator_view(books):
    _queued_txn("1000")
    _failed_txn()

    groups = {g["key"]: g for g in metrics.in_flight()["groups"]}
    assert groups["queued"]["count"] == 1 and groups["queued"]["amount"] == Decimal("1000")
    assert groups["check"]["count"] == 1


@pytest.mark.django_db
def test_health_flags_a_failure_newer_than_the_last_success(books):
    _completed_txn()
    rows = {r["label"]: r for r in metrics.health(float_level="ok")}
    assert rows["Retraits plopplop"]["status"] == "ok"

    _failed_txn()
    rows = {r["label"]: r for r in metrics.health(float_level="ok")}
    assert rows["Retraits plopplop"]["status"] == "warn"


@pytest.mark.django_db
def test_health_never_calls_a_provider(books):
    _queued_txn()
    with mock.patch(GET_CLIENT) as get_client:
        metrics.health(float_level="ok")
    get_client.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize("role, sees_fees", [(Role.SUPERADMIN, True), (Role.OPERATOR, False), (Role.SUPPORT, False)])
def test_platform_commission_is_shown_to_the_superadmin_only(client, books, role, sees_fees):
    _completed_txn()
    client.force_login(_user(role))
    html = client.get(reverse("console:dashboard")).content.decode()

    assert ('data-kpi="platform-fees"' in html) is sees_fees
    for panel in ('data-kpi="delivered"', 'data-chart="volume"', 'data-panel="health"', 'data-panel="in-flight"'):
        assert panel in html, panel


@pytest.mark.django_db
def test_chart_heights_stay_valid_css_in_french(client, books):
    _completed_txn("3000")
    _completed_txn("1000", source=Wallet.NATCASH, destination=Wallet.MONCASH)
    client.force_login(_user(Role.OPERATOR))
    html = client.get(reverse("console:dashboard")).content.decode()
    assert "height: 75.0%" in html and "75,0%" not in html
