"""Verrou des decaissements.

Scenario protege : deux processus de decaissement simultanes -- en
pratique l'ancienne et la nouvelle instance pendant un deploiement.
Un lot de 5 retraits espaces de 125 s dure bien plus longtemps que la
duree de vie du verrou : il doit rester detenu d'un bout a l'autre, sans
qu'un worker tue ne bloque la file indefiniment.
"""

from __future__ import annotations

import os
from decimal import Decimal
from unittest import mock

import pytest
from django.core.cache import cache

from apps.ledger import services as ledger
from apps.transactions import services, tasks
from apps.transactions.locks import LocalLockBackend, RedisLockBackend
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

TTL = 60
COOLDOWN = 125
PAYOUT_CALL_SECONDS = 20


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    LocalLockBackend.reset()
    cache.delete(tasks.LAST_PAYOUT_KEY)
    c = FakeClock()
    with mock.patch.object(tasks, "_now", c), mock.patch.object(tasks, "_sleep", c.sleep):
        yield c
    LocalLockBackend.reset()
    cache.delete(tasks.LAST_PAYOUT_KEY)


def _backend(clock):
    return LocalLockBackend(key="test:payout:lock", clock=clock)


def _lock_factory(backend):
    return mock.patch.object(tasks, "_new_lock", lambda: tasks.PayoutLock(ttl=TTL, backend=backend))


# ----------------------------------------------------------------------
# Contrat du verrou
# ----------------------------------------------------------------------
def test_lock_is_exclusive(clock):
    first, second = _backend(clock), _backend(clock)
    assert first.acquire(TTL) is True
    assert second.acquire(TTL) is False


def test_killed_holder_frees_the_queue_after_ttl(clock):
    dead, successor = _backend(clock), _backend(clock)
    dead.acquire(TTL)  # jamais rafraichi ni libere : worker tue

    clock.sleep(TTL - 1)
    assert successor.acquire(TTL) is False
    clock.sleep(2)
    assert successor.acquire(TTL) is True


def test_refresh_keeps_the_lock_well_beyond_its_ttl(clock):
    holder, other = _backend(clock), _backend(clock)
    holder.acquire(TTL)
    for _ in range(20):  # 20 x 50 s, plus de 16 fois la duree de vie
        clock.sleep(50)
        assert holder.refresh(TTL) is True
        assert other.acquire(TTL) is False


def test_expired_lock_taken_by_another_cannot_be_refreshed_or_released(clock):
    old, new = _backend(clock), _backend(clock)
    old.acquire(TTL)
    clock.sleep(TTL + 1)
    assert new.acquire(TTL) is True

    assert old.refresh(TTL) is False
    old.release()
    assert _backend(clock).acquire(TTL) is False  # le verrou du nouveau tient toujours


# ----------------------------------------------------------------------
# Lot de decaissement
# ----------------------------------------------------------------------
def _queued(n):
    ledger.ensure_accounts()
    for _ in range(n):
        txn = services.create_transaction(
            source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH,
            recipient_phone="50932123456", net_amount=Decimal("1000"),
        )
        txn.transition(State.AWAITING_PAYMENT)
        services.confirm_payment(txn, provider_amount=txn.total_charged)


@pytest.mark.django_db
def test_full_batch_holds_the_lock_from_start_to_end(clock, settings):
    settings.PAYOUT_COOLDOWN_SECONDS = COOLDOWN
    _queued(5)
    competitor_attempts = []

    def fake_payout(txn):
        # Pendant chaque retrait, un second processus tente sa chance.
        competitor_attempts.append(_backend(clock).acquire(TTL))
        clock.sleep(PAYOUT_CALL_SECONDS)
        txn.transition(State.PAYOUT_IN_FLIGHT)

    start = clock.now
    with _lock_factory(_backend(clock)), mock.patch("apps.transactions.services.execute_payout", side_effect=fake_payout):
        result = tasks.drain_payout_queue(max_batch=5)

    assert result == {"processed": 5}
    assert clock.now - start > 4 * COOLDOWN > TTL  # le lot a bien depasse la duree de vie
    assert competitor_attempts == [False] * 5
    assert not Transaction.objects.payable().exists()
    assert _backend(clock).acquire(TTL) is True  # libere a la fin du lot


@pytest.mark.django_db
def test_batch_stops_before_the_next_payout_when_the_lock_is_lost(clock, settings):
    settings.PAYOUT_COOLDOWN_SECONDS = COOLDOWN
    _queued(3)
    paid = []

    def fake_payout(txn):
        paid.append(txn.reference)
        txn.transition(State.PAYOUT_IN_FLIGHT)
        # Le processus gele au-dela de la duree de vie ; un autre prend la main.
        clock.sleep(TTL + 5)
        assert _backend(clock).acquire(TTL) is True

    with _lock_factory(_backend(clock)), mock.patch("apps.transactions.services.execute_payout", side_effect=fake_payout):
        result = tasks.drain_payout_queue(max_batch=3)

    assert result == {"processed": 1, "lock_lost": True}
    assert len(paid) == 1
    assert Transaction.objects.payable().count() == 2


@pytest.mark.django_db
def test_second_process_skips_while_a_batch_runs(clock):
    _backend(clock).acquire(TTL)
    with _lock_factory(_backend(clock)), mock.patch("apps.transactions.services.execute_payout") as execute:
        assert tasks.drain_payout_queue() == {"skipped": True}
    execute.assert_not_called()


def test_unreachable_lock_store_means_no_payout(clock):
    broken = mock.Mock()
    broken.acquire.side_effect = ConnectionError("redis injoignable")
    with tasks.PayoutLock(ttl=TTL, backend=broken) as lock:
        assert lock.acquired is False


# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------
def test_drain_task_expires_after_one_beat_interval(settings):
    entry = settings.CELERY_BEAT_SCHEDULE["drain-payouts"]
    assert entry["options"]["expires"] == settings.PAYOUT_DRAIN_INTERVAL_SECONDS
    assert entry["schedule"] == settings.PAYOUT_DRAIN_INTERVAL_SECONDS
    assert tasks.drain_payout_queue.expires == settings.PAYOUT_DRAIN_INTERVAL_SECONDS


def test_lock_ttl_covers_a_payout_but_is_never_wide(settings):
    # Un retrait complet = 3 appels HTTP, connexion + lecture chacun.
    assert settings.PAYOUT_LOCK_TTL_SECONDS > 6 * settings.PLOPPLOP["TIMEOUT"]
    assert settings.PAYOUT_LOCK_TTL_SECONDS < 600
    assert tasks.LOCK_REFRESH_STEP_SECONDS * 5 < settings.PAYOUT_LOCK_TTL_SECONDS


# ----------------------------------------------------------------------
# Redis reel (facultatif) : PLIPPLIP_TEST_REDIS_URL=redis://localhost:6379/15
# ----------------------------------------------------------------------
REDIS_URL = os.environ.get("PLIPPLIP_TEST_REDIS_URL")


@pytest.mark.skipif(not REDIS_URL, reason="PLIPPLIP_TEST_REDIS_URL non defini")
def test_redis_backend_honours_the_same_contract():
    import time

    key = "plipplip:test:payout:lock"
    first, second = RedisLockBackend(REDIS_URL, key=key), RedisLockBackend(REDIS_URL, key=key)
    first._client.delete(key)
    try:
        assert first.acquire(2) is True
        assert second.acquire(2) is False
        time.sleep(1.2)
        assert first.refresh(2) is True
        time.sleep(1.2)  # sans le rafraichissement, le verrou aurait expire
        assert second.acquire(2) is False

        time.sleep(2.2)  # plus de rafraichissement : expiration
        assert second.acquire(2) is True
        assert first.refresh(2) is False
        first.release()
        assert RedisLockBackend(REDIS_URL, key=key).acquire(2) is False
    finally:
        first._client.delete(key)
