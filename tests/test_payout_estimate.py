"""Delai de decaissement annonce au client.

Regles protegees :
  - un seul calcul de rang, partage par la console, l'API et le site ;
  - jamais de rang ni de profondeur cote client ;
  - fourchette arrondie vers le haut ; promesse fixee a l'entree en file,
    jamais elargie, retiree definitivement si la realite la depasse ;
  - pas de duree si le float ne couvre pas, au-dela du plafond, ou si le
    worker de decaissement est a l'arret -- avec alerte console ;
  - rafraichissement degressif de la page de suivi.
"""

from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import services as accounts
from apps.accounts.models import Role, User
from apps.api.status import public_wait
from apps.console.views import _queue_context
from apps.ledger import services as ledger
from apps.providers.plopplop import exceptions as pp
from apps.transactions import queue, services
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State
from apps.web.polling import poll_interval

APPS_DIR = Path(__file__).resolve().parent.parent / "apps"
GET_CLIENT = "apps.transactions.services.get_client"
CODE = "123456"
PHONE = "50937123456"
COOLDOWN = 125
NO_DURATION = {"available": False, "min_minutes": None, "max_minutes": None}


@pytest.fixture(autouse=True)
def setup(db, settings):
    ledger.ensure_accounts()
    settings.PAYOUT_COOLDOWN_SECONDS = COOLDOWN
    settings.PAYOUT_DRAIN_INTERVAL_SECONDS = 30
    settings.PAYOUT_STALL_SECONDS = 3 * COOLDOWN
    settings.ETA_MAX_DISPLAY_SECONDS = 3600
    cache.clear()
    yield
    cache.clear()


def _at(moment):
    return mock.patch("django.utils.timezone.now", return_value=moment)


def _customer():
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        accounts.request_code(PHONE)
    customer, raw, _ = accounts.verify_code(PHONE, CODE)
    return customer, raw


def _web_login(client):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:login"), {"phone": PHONE})
    client.post(reverse("web:login_code"), {"code": CODE})


def _api(raw):
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return api


def _queued(customer=None):
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH,
        recipient_phone="50932123456", net_amount=Decimal("1000"), customer=customer,
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()
    return txn


def _fail(txn):
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.side_effect = pp.InsufficientBalance(
            "Solde epuise", code="INSUFFICIENT_BALANCE", status=400
        )
        services.execute_payout(txn)
    txn.refresh_from_db()
    assert txn.state == State.PAYOUT_FAILED


def _display(txn):
    txn.refresh_from_db()
    return queue.estimated_wait(txn)["display"]


def _drain_float():
    ledger.post(
        reference="SORTIE-TEST",
        description="Retrait de tresorerie",
        lines=[(ledger.FLOAT, Decimal("-1000000"), ""), (ledger.CASH_SETTLEMENT, Decimal("1000000"), "")],
    )


def _all_keys(node) -> set:
    if isinstance(node, dict):
        return set(node) | set().union(*(_all_keys(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_all_keys(v) for v in node)) if node else set()
    return set()


# ----------------------------------------------------------------------
# Tranches
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "seconds, expected",
    [
        (0, (0, 5)), (300, (0, 5)), (301, (5, 10)), (600, (5, 10)), (601, (10, 15)),
        (1800, (25, 30)), (1801, (30, 45)), (2700, (30, 45)), (2701, (45, 60)), (3600, (45, 60)),
    ],
)
def test_ranges_round_up_to_the_next_bucket(seconds, expected):
    display = queue.bucket_range(queue.bucket_upper_minutes(seconds))
    assert (display["min_minutes"], display["max_minutes"]) == expected


# ----------------------------------------------------------------------
# Promesse
# ----------------------------------------------------------------------
def test_promise_is_fixed_at_queue_entry_and_kept_on_requeue():
    txn = _queued()
    promised = txn.payout_eta_deadline
    assert promised is not None

    _fail(txn)
    services.retry_failed_payout(txn)

    txn.refresh_from_db()
    assert (txn.state, txn.payout_eta_deadline) == (State.PAYOUT_QUEUED, promised)


def test_range_is_never_widened_and_is_withdrawn_once_exceeded():
    ahead = [_queued(), _queued()]
    for txn in ahead:
        _fail(txn)
    target = _queued()
    assert _display(target) == {"min_minutes": 0, "max_minutes": 5}

    services.retry_failed_payout(ahead[0])  # repris devant : confirmation plus ancienne
    assert _display(target) == {"min_minutes": 0, "max_minutes": 5}  # pas elargi

    services.retry_failed_payout(ahead[1])  # les 5 minutes promises ne tiennent plus
    assert _display(target) is None
    target.refresh_from_db()
    assert target.payout_eta_withdrawn is True

    for txn in ahead:
        txn.transition(State.PAYOUT_IN_FLIGHT)
    assert queue.estimated_wait(target)["seconds"] == COOLDOWN  # de nouveau premier...
    assert _display(target) is None  # ... la promesse retiree ne revient pas


def test_range_only_steps_down_as_the_queue_moves():
    t0 = timezone.now()
    with _at(t0):
        txns = [_queued() for _ in range(6)]
        seen = [_display(txns[-1])["max_minutes"]]
    for minute, head in zip((2, 4, 6, 8, 10), txns[:5]):
        with _at(t0 + timedelta(minutes=minute)):
            head.transition(State.PAYOUT_IN_FLIGHT)
            seen.append(_display(txns[-1])["max_minutes"])
    with _at(t0 + timedelta(minutes=12)):
        txns[-1].transition(State.PAYOUT_IN_FLIGHT)
        seen.append(_display(txns[-1])["max_minutes"])

    assert seen == [20, 20, 20, 15, 15, 10, 10]


# ----------------------------------------------------------------------
# Cas sans duree
# ----------------------------------------------------------------------
def test_uncovered_transfer_gets_no_duration(client):
    customer, raw = _customer()
    covered_then = _queued(customer)
    _drain_float()
    uncovered_from_start = _queued(customer)

    for txn in (covered_then, uncovered_from_start):
        estimate = queue.estimated_wait(txn)
        assert (estimate["covered"], estimate["display"]) == (False, None)
    assert uncovered_from_start.payout_eta_deadline is None

    body = _api(raw).get(reverse("api:transfer_detail", args=[covered_then.reference])).json()
    assert body["estimated_wait"] == NO_DURATION
    _web_login(client)
    html = client.get(reverse("web:transfer_status", args=[covered_then.reference])).content.decode()
    assert 'data-wait="none"' in html and "minutes" not in html


def test_estimate_beyond_the_cap_is_not_shown(settings):
    settings.ETA_MAX_DISPLAY_SECONDS = 600
    txns = [_queued() for _ in range(5)]

    assert _display(txns[0]) == {"min_minutes": 0, "max_minutes": 5}
    assert _display(txns[2]) == {"min_minutes": 5, "max_minutes": 10}
    assert txns[4].payout_eta_deadline is None
    assert _display(txns[4]) is None


def test_payout_in_flight_is_announced_as_less_than_five_minutes():
    txn = _queued()
    txn.transition(State.PAYOUT_IN_FLIGHT)
    assert _display(txn) == {"min_minutes": 0, "max_minutes": 5}


@pytest.mark.parametrize("trouble", ["unknown", "failed", "held"])
def test_payout_trouble_never_shows_a_duration(trouble):
    if trouble == "held":
        txn = services.create_transaction(
            source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH,
            recipient_phone="50932123456", net_amount=Decimal("1000"),
        )
        txn.transition(State.AWAITING_PAYMENT)
        services.confirm_payment(txn, provider_amount=Decimal("100"))
    else:
        txn = _queued()
        if trouble == "failed":
            _fail(txn)
        else:
            with mock.patch(GET_CLIENT) as get_client:
                get_client.return_value.withdraw.side_effect = pp.PlopPlopIndeterminate("timeout")
                services.execute_payout(txn)
    txn.refresh_from_db()

    assert public_wait(txn) == NO_DURATION


def test_stalled_payout_worker_hides_the_duration_and_alerts_the_console(client, settings):
    settings.PAYOUT_STALL_SECONDS = 60
    t0 = timezone.now()
    with _at(t0):
        txns = [_queued() for _ in range(4)]
    target = txns[-1]
    client.force_login(User.objects.create_user(username="op", password="x", role=Role.OPERATOR))

    with _at(t0 + timedelta(minutes=2)):
        assert _display(target) is None
        target.refresh_from_db()
        assert target.payout_eta_withdrawn is False  # masquee, pas retiree
        assert 'data-console-alert="payout-worker"' in client.get(reverse("console:dashboard")).content.decode()

        txns[0].transition(State.PAYOUT_IN_FLIGHT)  # le worker repart

        assert _display(target) == {"min_minutes": 10, "max_minutes": 15}
        assert "data-console-alert" not in client.get(reverse("console:dashboard")).content.decode()


def test_console_alert_is_never_shown_to_anonymous_visitors(client, settings):
    settings.PAYOUT_STALL_SECONDS = 60
    t0 = timezone.now()
    with _at(t0):
        _queued()
    with _at(t0 + timedelta(minutes=10)):
        assert queue.payout_worker_status().stalled is True
        for url in (reverse("console:login"), reverse("web:home")):
            assert "data-console-alert" not in client.get(url).content.decode(), url


# ----------------------------------------------------------------------
# Rien d'exploitation cote client
# ----------------------------------------------------------------------
def test_client_surfaces_never_expose_rank_or_depth(client):
    customer, raw = _customer()
    txns = [_queued(customer) for _ in range(7)]
    target = txns[-1]
    forbidden = {"rank", "position", "depth", "queue", "stall_rank", "seconds", "eta_seconds", "covered"}
    api = _api(raw)

    detail = api.get(reverse("api:transfer_detail", args=[target.reference])).json()
    listing = api.get(reverse("api:transfers")).json()

    assert detail["estimated_wait"] == {"available": True, "min_minutes": 15, "max_minutes": 20}
    assert not _all_keys(detail) & forbidden
    assert not _all_keys(listing) & forbidden

    _web_login(client)
    for url in (reverse("web:transfer_status", args=[target.reference]), reverse("web:transfer_detail", args=[target.reference])):
        text = re.sub(r"<[^>]+>", " ", client.get(url).content.decode())
        assert "environ 15 à 20 minutes" in text
        # Le rang et la profondeur valent 7 : aucun « 7 » isole hors de la
        # reference aleatoire (montants et dates ont leurs chiffres colles).
        visible = text.replace(target.reference, "")
        assert not re.search(r"(?<![\d ,:/])7(?![\d ,:/])", visible), url
        assert [w for w in ("rang", "position", "file d'attente", "devant vous") if w in visible.lower()] == []


def test_console_and_client_use_the_same_rank():
    txns = [_queued() for _ in range(4)]
    rows = {row["txn"].pk: row for row in _queue_context()["rows"]}
    snapshot = queue.queue_snapshot()

    for position, txn in enumerate(txns, start=1):
        estimate = queue.estimated_wait(txn, snapshot)
        assert rows[txn.pk]["rank"] == position
        assert estimate["seconds"] == rows[txn.pk]["eta_seconds"] == position * COOLDOWN
        assert estimate["covered"] == rows[txn.pk]["covered"]


def test_simultaneous_confirmations_keep_one_stable_order():
    first, second = _queued(), _queued()
    Transaction.objects.filter(pk__in=[first.pk, second.pk]).update(payment_confirmed_at=first.payment_confirmed_at)

    ranks = [e.txn.pk for e in queue.queue_snapshot().entries]
    worker_order = list(Transaction.objects.payable().values_list("pk", flat=True))
    assert ranks == worker_order == sorted([first.pk, second.pk])


def test_rank_is_computed_in_a_single_place():
    offenders = sorted(
        path.relative_to(APPS_DIR).as_posix()
        for path in APPS_DIR.rglob("*.py")
        if "payable()" in (source := path.read_text(encoding="utf-8")) and "enumerate(" in source
    )
    assert offenders == ["transactions/queue.py"]
    assert "enumerate(" not in (APPS_DIR / "console" / "views.py").read_text(encoding="utf-8")


# ----------------------------------------------------------------------
# Performance et rafraichissement
# ----------------------------------------------------------------------
def test_transfer_list_reads_the_queue_once_whatever_its_length():
    customer, raw = _customer()
    api = _api(raw)
    _queued(customer)
    api.get(reverse("api:transfers"))  # chauffe : derniere utilisation du jeton

    with CaptureQueriesContext(connection) as one:
        api.get(reverse("api:transfers"))
    for _ in range(4):
        _queued(customer)
    with CaptureQueriesContext(connection) as five:
        response = api.get(reverse("api:transfers"))

    assert [item["estimated_wait"]["available"] for item in response.json()["results"]] == [True] * 5
    assert len(five.captured_queries) == len(one.captured_queries)


@pytest.mark.parametrize(
    "status, elapsed, expected",
    [
        ("awaiting_payment", 0, 5), ("awaiting_payment", 3600, 5),
        ("in_progress", 10, 5), ("in_progress", 59, 5), ("in_progress", 60, 15), ("in_progress", 299, 15),
        ("in_progress", 300, 30), ("in_progress", 2400, 30),
        ("delivered", 10, None), ("expired", 10, None), ("refunded", 10, None), ("cancelled", 10, None),
    ],
)
def test_status_page_polling_backs_off(status, elapsed, expected):
    now = timezone.now()
    txn = SimpleNamespace(payment_confirmed_at=now - timedelta(seconds=elapsed))
    assert poll_interval(txn, status, now) == expected


def test_status_fragment_renders_the_backed_off_interval(client):
    customer, _ = _customer()
    t0 = timezone.now()
    with _at(t0):
        txn = _queued(customer)
    _web_login(client)

    with _at(t0 + timedelta(minutes=10)):
        html = client.get(reverse("web:transfer_status", args=[txn.reference])).content.decode()
    assert 'hx-trigger="every 30s"' in html
