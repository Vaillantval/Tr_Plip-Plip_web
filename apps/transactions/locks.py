"""Verrou exclusif des decaissements.

Duree de vie COURTE, rafraichie par le detenteur a chaque etape du lot.
Un timeout long aurait masque le probleme (un lot de 5 retraits espaces
de 125 s depasse largement 300 s) tout en rendant un worker tue
indefiniment bloquant. Ici un worker tue libere la file au plus tard
apres PAYOUT_LOCK_TTL_SECONDS.

Chaque acquisition porte un jeton unique : rafraichir ou liberer un
verrou qui appartient desormais a un autre processus est refuse. Sur
Redis, ces controles sont atomiques (scripts Lua de redis-py).
"""

from __future__ import annotations

import threading
import time
import uuid

from django.conf import settings

PAYOUT_LOCK_KEY = "plipplip:payout:lock"


class RedisLockBackend:
    """Production : partage entre tous les processus et conteneurs."""

    def __init__(self, url: str, key: str = PAYOUT_LOCK_KEY):
        import redis

        self._client = redis.Redis.from_url(url)
        self._key = key
        self._lock = None

    def acquire(self, ttl: float) -> bool:
        self._lock = self._client.lock(self._key, timeout=ttl, blocking=False, thread_local=False)
        return bool(self._lock.acquire(blocking=False))

    def refresh(self, ttl: float) -> bool:
        from redis.exceptions import LockError

        try:
            # reacquire() remet la duree de vie a `timeout`, seulement si le
            # jeton stocke est toujours le notre.
            self._lock.reacquire()
        except LockError:
            return False
        return True

    def release(self) -> None:
        from redis.exceptions import LockError

        try:
            self._lock.release()
        except LockError:
            pass  # expire ou repris par un autre : rien a liberer


class LocalLockBackend:
    """Developpement et tests : memoire du processus, meme contrat que Redis."""

    _entries: dict[str, tuple[str, float]] = {}
    _mutex = threading.Lock()

    def __init__(self, key: str = PAYOUT_LOCK_KEY, clock=time.monotonic):
        self._key = key
        self._clock = clock
        self._token = uuid.uuid4().hex

    def _alive(self, now: float):
        entry = self._entries.get(self._key)
        if entry is not None and entry[1] <= now:
            del self._entries[self._key]
            return None
        return entry

    def acquire(self, ttl: float) -> bool:
        with self._mutex:
            now = self._clock()
            if self._alive(now) is not None:
                return False
            self._entries[self._key] = (self._token, now + ttl)
            return True

    def refresh(self, ttl: float) -> bool:
        with self._mutex:
            now = self._clock()
            entry = self._alive(now)
            if entry is None or entry[0] != self._token:
                return False
            self._entries[self._key] = (self._token, now + ttl)
            return True

    def release(self) -> None:
        with self._mutex:
            entry = self._alive(self._clock())
            if entry is not None and entry[0] == self._token:
                del self._entries[self._key]

    @classmethod
    def reset(cls) -> None:
        with cls._mutex:
            cls._entries.clear()


def default_backend():
    backend = settings.CACHES["default"]["BACKEND"]
    if backend.endswith("RedisCache"):
        return RedisLockBackend(settings.REDIS_URL)
    return LocalLockBackend()
