"""Verrouillage apres echecs de mot de passe.

Deux compteurs, independants :

PAR COMPTE -- la protection. En base (Customer.password_failures,
password_locked_until) : une eviction Redis ne doit pas rendre un compte
de nouveau attaquable. A partir du seuil, chaque echec pose un verrou qui
double : 1, 2, 4, 8, 16, 32 min, plafonne. Un verrou ne bloque QUE le mot
de passe : le code SMS et la reinitialisation restent ouverts, sinon
n'importe qui priverait un client de son compte en tapant de faux mots
de passe. Un numero INCONNU n'a pas de compteur : son mot de passe ne
peut de toute facon jamais etre bon, et la reponse est identique.

PAR IP -- detection d'abus, pas protection individuelle. En cache :
c'est un signal souple, le perdre n'ouvre aucun compte. Seuil haut (NAT
operateur, WiFi partage). Chaque blocage laisse une trace lisible en
console (CustomerAuthEvent ip_locked).

Rien ici ne decide du message affiche : il est le meme dans tous les cas.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db.models import F
from django.utils import timezone

from .models import AuthEvent, Customer, CustomerAuthEvent

#: Duree de memoire du rang de blocage d'une IP (pour le doublement).
IP_LEVEL_TTL = 24 * 3600


def _conf(key: str) -> int:
    return settings.PASSWORD_LOCKOUT[key]


def _delay(failures_past_threshold: int, base: int, cap: int) -> int:
    return min(base * 2 ** max(failures_past_threshold, 0), cap)


# ----------------------------------------------------------------------
# Par compte
# ----------------------------------------------------------------------
def account_locked(customer: Customer, now=None) -> bool:
    now = now or timezone.now()
    return bool(customer.password_locked_until and customer.password_locked_until > now)


def account_failed(customer: Customer, *, ip: str, now=None) -> int | None:
    """Compte un echec. Retourne la duree du verrou pose, en secondes, ou None.

    Increment atomique en base : deux essais simultanes comptent pour deux.
    """
    now = now or timezone.now()
    Customer.objects.filter(pk=customer.pk).update(password_failures=F("password_failures") + 1)
    failures = Customer.objects.values_list("password_failures", flat=True).get(pk=customer.pk)
    threshold = _conf("ACCOUNT_THRESHOLD")
    if failures < threshold:
        return None
    seconds = _delay(failures - threshold, _conf("ACCOUNT_BASE_SECONDS"), _conf("ACCOUNT_MAX_SECONDS"))
    Customer.objects.filter(pk=customer.pk).update(password_locked_until=now + timedelta(seconds=seconds))
    CustomerAuthEvent.objects.create(
        phone=customer.phone,
        customer=customer,
        event=AuthEvent.ACCOUNT_LOCKED,
        ip_address=ip or None,
        detail={"seconds": seconds, "failures": failures},
    )
    return seconds


def account_clear(customer: Customer) -> None:
    """Remise a zero : connexion reussie (mot de passe ou code), reinitialisation."""
    if customer.password_failures or customer.password_locked_until:
        Customer.objects.filter(pk=customer.pk).update(password_failures=0, password_locked_until=None)
        customer.password_failures, customer.password_locked_until = 0, None


# ----------------------------------------------------------------------
# Par IP
# ----------------------------------------------------------------------
def _key(kind: str, ip: str) -> str:
    return f"plipplip:pwlock:ip:{kind}:{ip}"


def ip_blocked(ip: str) -> bool:
    return bool(ip) and cache.get(_key("until", ip)) is not None


def ip_failed(ip: str, *, phone: str = "") -> int | None:
    """Compte un echec pour l'IP. Retourne la duree du blocage pose, ou None."""
    if not ip:
        return None
    window = _conf("IP_WINDOW_SECONDS")
    key = _key("count", ip)
    if cache.add(key, 1, window):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:  # expiree entre add et incr
            cache.set(key, 1, window)
            count = 1
    if count < _conf("IP_THRESHOLD"):
        return None
    level_key = _key("level", ip)
    level = (cache.get(level_key) or 0) + 1
    cache.set(level_key, level, IP_LEVEL_TTL)
    seconds = _delay(level - 1, _conf("IP_BASE_SECONDS"), _conf("IP_MAX_SECONDS"))
    cache.set(_key("until", ip), 1, seconds)
    # Nouvelle fenetre apres le blocage : sinon chaque echec suivant
    # reposerait un blocage et ferait monter le rang en rafale.
    cache.delete(key)
    CustomerAuthEvent.objects.create(
        phone=phone,
        event=AuthEvent.IP_LOCKED,
        ip_address=ip,
        detail={"seconds": seconds, "level": level, "failures": count},
    )
    return seconds
