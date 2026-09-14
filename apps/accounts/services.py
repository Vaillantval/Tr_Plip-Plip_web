"""Identification des clients de l'API : OTP par SMS et jetons d'acces."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone

from . import otp
from .models import Customer, CustomerToken
from .phone import normalize

TOKEN_PREFIX = "ppk_"
#: Frequence maximale d'ecriture de last_used_at, pour ne pas ecrire a
#: chaque requete de suivi de statut.
LAST_USED_RESOLUTION = timedelta(minutes=5)


class InvalidCode(Exception):
    pass


class CustomerDisabled(Exception):
    pass


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def request_code(phone: str) -> str:
    """Envoie un code au numero. Retourne le numero normalise.

    Un client desactive ne recoit rien, mais la reponse ne le distingue
    pas : l'API ne revele pas quels numeros sont connus.
    """
    phone = normalize(phone)
    if Customer.objects.filter(phone=phone, is_active=False).exists():
        return phone
    otp.get_backend().send(phone)
    return phone


@db_transaction.atomic
def authenticate_code(phone: str, code: str) -> Customer:
    """Valide le code et cree le client au premier passage. N'emet aucun jeton.

    Utilise tel quel par le site web (session), et par verify_code pour l'API.
    """
    phone = normalize(phone)
    code = (code or "").strip()
    if not code.isdigit() or not otp.get_backend().check(phone, code):
        raise InvalidCode("Code invalide ou expire")

    customer, _ = Customer.objects.get_or_create(phone=phone)
    if not customer.is_active:
        raise CustomerDisabled("Compte desactive")
    Customer.objects.filter(pk=customer.pk).update(last_login_at=timezone.now())
    return customer


@db_transaction.atomic
def verify_code(phone: str, code: str) -> tuple[Customer, str, CustomerToken]:
    """Valide le code, cree le client au premier passage, emet un jeton d'API.

    Le jeton en clair n'est retourne qu'ici : il n'est stocke que hache.
    """
    customer = authenticate_code(phone, code)
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = CustomerToken.objects.create(
        customer=customer,
        key_hash=_hash(raw),
        prefix=raw[:12],
        expires_at=timezone.now() + timedelta(seconds=settings.API_TOKEN_TTL_SECONDS),
    )
    return customer, raw, token


def authenticate_token(raw_token: str) -> tuple[Customer, CustomerToken] | None:
    if not raw_token or not raw_token.startswith(TOKEN_PREFIX):
        return None
    now = timezone.now()
    token = (
        CustomerToken.objects.select_related("customer")
        .filter(key_hash=_hash(raw_token), revoked_at__isnull=True, expires_at__gt=now, customer__is_active=True)
        .first()
    )
    if token is None:
        return None
    if token.last_used_at is None or now - token.last_used_at > LAST_USED_RESOLUTION:
        CustomerToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return token.customer, token


def revoke_token(token: CustomerToken) -> None:
    CustomerToken.objects.filter(pk=token.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
