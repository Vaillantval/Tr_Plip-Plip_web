"""Identification des clients : code SMS, mot de passe, jetons d'acces.

L'OTP fait deux choses qu'on voit d'habitude ensemble : prouver qu'on
CONTROLE le numero, et prouver qu'on est celui qui se connecte. Un mot de
passe ne remplace que la seconde. D'ou le decoupage :

    verify_phone()            -> VerifiedPhone   (seule preuve de controle)
    _customer_for_verified()  -> le SEUL endroit qui cree un Customer
    _customer_for_password()  -> ne cree JAMAIS rien
    _admit()                  -> commun aux deux : is_active, langue, date

Un numero inconnu passe donc toujours par un code. Garde par
tests/test_customer_password.py, qui inspecte le code source.

Les services ne touchent ni a la requete ni a la session : ils recoivent
des valeurs simples (dont l'IP) et levent des exceptions typees.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from functools import lru_cache

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import get_supported_language_variant

from . import lockout, otp, passwords
from .models import AuthEvent, AuthMethod, Customer, CustomerAuthEvent, CustomerToken
from .passwords import PasswordRejected
from .phone import InvalidPhone, normalize

TOKEN_PREFIX = "ppk_"
#: Frequence maximale d'ecriture de last_used_at, pour ne pas ecrire a
#: chaque requete de suivi de statut.
LAST_USED_RESOLUTION = timedelta(minutes=5)

__all__ = [
    "AuthFailed",
    "CustomerDisabled",
    "InvalidCode",
    "PasswordRejected",
    "WrongCurrentPassword",
]


class InvalidCode(Exception):
    pass


class CustomerDisabled(Exception):
    pass


class AuthFailed(Exception):
    """Connexion par mot de passe refusee.

    UNE seule exception, UN seul message, quelle que soit la cause : numero
    inconnu, numero sans mot de passe, mot de passe faux, compte ou IP
    verrouilles. Distinguer ces cas revelerait la liste des clients.
    """


class WrongCurrentPassword(Exception):
    """Changement de mot de passe : l'ancien saisi est faux."""


@dataclass(frozen=True)
class VerifiedPhone:
    """Preuve qu'un code envoye a ce numero vient d'etre saisi.

    Ne se fabrique que dans verify_phone(). C'est le seul objet qui ouvre
    le droit de creer un Customer.
    """

    phone: str


def _hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Hache factice, calcule une fois. Verifie quand il n'y a rien a
    verifier (numero inconnu, sans mot de passe, verrouille) : sans lui,
    la duree de reponse trahirait les comptes existants."""
    return make_password(secrets.token_urlsafe(16))


def _event(event: str, *, phone: str = "", customer: Customer | None = None, ip: str = "", **detail) -> None:
    CustomerAuthEvent.objects.create(
        phone=phone, customer=customer, event=event, ip_address=ip or None, detail=detail
    )


# ----------------------------------------------------------------------
# Code SMS
# ----------------------------------------------------------------------
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


def verify_phone(phone: str, code: str) -> VerifiedPhone:
    """Seule fabrique de VerifiedPhone. Consomme le code."""
    phone = normalize(phone)
    code = (code or "").strip()
    if not code.isdigit() or not otp.get_backend().check(phone, code):
        raise InvalidCode("Code invalide ou expire")
    return VerifiedPhone(phone=phone)


def _customer_for_verified(verified: VerifiedPhone) -> Customer:
    """LE seul endroit du projet qui cree un Customer."""
    if not isinstance(verified, VerifiedPhone):
        raise TypeError("Un client ne se cree que pour un numero verifie par code")
    customer, _ = Customer.objects.get_or_create(phone=verified.phone)
    return customer


def normalize_language(value: str) -> str:
    """Langue supportee, ou chaine vide. Ne leve jamais."""
    try:
        code = get_supported_language_variant(str(value or ""))
    except LookupError:
        return ""
    return code if code in dict(settings.LANGUAGES) else ""


def _admit(customer: Customer, *, language: str = "") -> Customer:
    """Ce qui suit TOUTE preuve, code ou mot de passe.

    `language` est retenu sur le client : c'est la seule trace durable de
    la langue qu'il lit, et donc la seule dont disposera une tache Celery
    pour rediger un SMS. Consequence assumee : les notifications parlent
    la langue de la DERNIERE CONNEXION.
    """
    if not customer.is_active:
        raise CustomerDisabled("Compte desactive")
    fields = {"last_login_at": timezone.now()}
    chosen = normalize_language(language)
    if chosen and chosen != customer.language:
        fields["language"] = chosen
        customer.language = chosen
    Customer.objects.filter(pk=customer.pk).update(**fields)
    return customer


@db_transaction.atomic
def authenticate_code(phone: str, code: str, *, language: str = "") -> Customer:
    """Valide le code et cree le client au premier passage. N'emet aucun jeton.

    Utilise tel quel par le site web (session), et par verify_code pour l'API.
    """
    customer = _customer_for_verified(verify_phone(phone, code))
    customer = _admit(customer, language=language)
    # Le code prouve la possession du numero : le compteur d'echecs de mot
    # de passe repart de zero.
    lockout.account_clear(customer)
    return customer


def _issue_token(customer: Customer, method: str) -> tuple[str, CustomerToken]:
    """Le jeton en clair n'est retourne qu'ici : il n'est stocke que hache.
    Meme duree quelle que soit la methode."""
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = CustomerToken.objects.create(
        customer=customer,
        key_hash=_hash(raw),
        prefix=raw[:12],
        auth_method=method,
        expires_at=timezone.now() + timedelta(seconds=settings.API_TOKEN_TTL_SECONDS),
    )
    return raw, token


@db_transaction.atomic
def verify_code(phone: str, code: str, *, language: str = "") -> tuple[Customer, str, CustomerToken]:
    """Valide le code, cree le client au premier passage, emet un jeton d'API."""
    customer = authenticate_code(phone, code, language=language)
    raw, token = _issue_token(customer, AuthMethod.CODE)
    return customer, raw, token


# ----------------------------------------------------------------------
# Mot de passe
# ----------------------------------------------------------------------
def _customer_for_password(phone: str) -> Customer | None:
    """Lecture seule. Ne cree JAMAIS de client : un numero inconnu n'a pas
    prouve qu'il etait controle par quelqu'un."""
    return Customer.objects.filter(phone=phone).first()


def authenticate_password(phone: str, password: str, *, ip: str = "", language: str = "") -> Customer:
    """Connexion par mot de passe. Leve AuthFailed, identique dans tous les
    cas d'echec, ou CustomerDisabled APRES un mot de passe correct (comme
    pour le code : le statut du compte ne se revele qu'apres la preuve)."""
    password = password or ""
    try:
        phone = normalize(phone)
    except InvalidPhone:
        check_password(password, _dummy_hash())
        raise AuthFailed("Numero ou mot de passe incorrect") from None

    if lockout.ip_blocked(ip):
        check_password(password, _dummy_hash())
        raise AuthFailed("Numero ou mot de passe incorrect")

    customer = _customer_for_password(phone)
    usable = customer is not None and customer.has_password and len(password) <= passwords.MAX_LENGTH
    if usable and lockout.account_locked(customer):
        # Verrou : on ne verifie rien, mais on prend le meme temps. Pas de
        # nouvel echec compte : un client qui retape pendant le verrou ne
        # doit pas le voir s'allonger.
        check_password(password, _dummy_hash())
        lockout.ip_failed(ip, phone=phone)
        raise AuthFailed("Numero ou mot de passe incorrect")

    if not usable:
        check_password(password, _dummy_hash())
        ok = False
    else:
        ok = check_password(password, customer.password, setter=lambda raw: _store_hash(customer, raw))

    if not ok:
        _event(AuthEvent.PASSWORD_FAILED, phone=phone, customer=customer, ip=ip)
        if usable:
            lockout.account_failed(customer, ip=ip)
        lockout.ip_failed(ip, phone=phone)
        raise AuthFailed("Numero ou mot de passe incorrect")

    customer = _admit(customer, language=language)
    lockout.account_clear(customer)
    return customer


def _store_hash(customer: Customer, raw: str) -> None:
    """Rehachage transparent quand l'algorithme par defaut change."""
    encoded = make_password(raw)
    Customer.objects.filter(pk=customer.pk).update(password=encoded)
    customer.password = encoded


def login_password(phone: str, password: str, *, ip: str = "", language: str = "") -> tuple[Customer, str, CustomerToken]:
    """Connexion par mot de passe pour l'API : emet un jeton."""
    customer = authenticate_password(phone, password, ip=ip, language=language)
    raw, token = _issue_token(customer, AuthMethod.PASSWORD)
    return customer, raw, token


def _revoke_all_tokens(customer: Customer, *, keep: CustomerToken | None = None) -> None:
    tokens = CustomerToken.objects.filter(customer=customer, revoked_at__isnull=True)
    if keep is not None:
        tokens = tokens.exclude(pk=keep.pk)
    tokens.update(revoked_at=timezone.now())


def _write_password(customer: Customer, password: str, *, bump_epoch: bool) -> None:
    fields = {"password": make_password(password), "password_set_at": timezone.now()}
    customer.password, customer.password_set_at = fields["password"], fields["password_set_at"]
    if bump_epoch:
        customer.auth_epoch += 1
        fields["auth_epoch"] = customer.auth_epoch
    Customer.objects.filter(pk=customer.pk).update(**fields)


@db_transaction.atomic
def set_password(
    customer: Customer,
    password: str,
    *,
    current: str | None = None,
    ip: str = "",
    keep_token: CustomerToken | None = None,
) -> Customer:
    """Creer (premiere fois) ou changer son mot de passe, client connecte.

    Changer exige l'ancien. Un changement coupe les AUTRES sessions et
    jetons ; l'appelant renouvelle la sienne (nouvelle valeur d'epoch).
    Une creation ne coupe rien : aucun mot de passe n'existait a voler.
    """
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    passwords.check(password, customer.phone)
    changing = customer.has_password
    if changing and not check_password(current or "", customer.password):
        raise WrongCurrentPassword("Mot de passe actuel incorrect")
    _write_password(customer, password, bump_epoch=changing)
    if changing:
        _revoke_all_tokens(customer, keep=keep_token)
    _event(AuthEvent.PASSWORD_CHANGED if changing else AuthEvent.PASSWORD_SET, phone=customer.phone, customer=customer, ip=ip)
    return customer


@db_transaction.atomic
def reset_password(phone: str, code: str, password: str, *, ip: str = "", language: str = "") -> Customer:
    """Mot de passe oublie : un code SMS, puis le nouveau mot de passe.

    Le mot de passe est valide AVANT de consommer le code : sinon un refus
    de regle obligerait a redemander un SMS (qui coute). Reussite : toutes
    les sessions web (epoch) et tous les jetons sont invalides, le compteur
    d'echecs repart de zero. Un numero inconnu devient un compte : il vient
    de prouver qu'il controle le numero.
    """
    phone = normalize(phone)
    passwords.check(password, phone)
    customer = _customer_for_verified(verify_phone(phone, code))
    customer = Customer.objects.select_for_update().get(pk=customer.pk)
    _write_password(customer, password, bump_epoch=True)
    _revoke_all_tokens(customer)
    lockout.account_clear(customer)
    _event(AuthEvent.PASSWORD_RESET, phone=phone, customer=customer, ip=ip)
    return _admit(customer, language=language)


def reset_password_token(phone: str, code: str, password: str, *, ip: str = "", language: str = "") -> tuple[Customer, str, CustomerToken]:
    """Reinitialisation pour l'API. Le nouveau jeton est note `code` : c'est
    le code qui a servi de preuve."""
    customer = reset_password(phone, code, password, ip=ip, language=language)
    raw, token = _issue_token(customer, AuthMethod.CODE)
    return customer, raw, token


# ----------------------------------------------------------------------
# Jetons
# ----------------------------------------------------------------------
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
