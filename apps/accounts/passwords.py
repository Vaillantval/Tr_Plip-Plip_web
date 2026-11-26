"""Regles du mot de passe client : les validateurs de Django, rien de plus.

Pas de regle sur les caracteres speciaux : elle degrade l'usage sur un
telephone sans gain reel. Seule la longueur minimale est abaissee (6).

Les validateurs sont appeles ici, un par un, et chaque refus est rendu
sous forme de CODE : le texte affiche au client est redige par la couche
qui parle au client (site, API), dans sa langue. Les messages de Django
n'ont pas de traduction creole.
"""

from __future__ import annotations

from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth import password_validation as pv
from django.core.exceptions import ValidationError

#: Au-dela, refus : un hachage sur une entree immense coute du temps serveur.
MAX_LENGTH = 128

TOO_SHORT = "too_short"
TOO_LONG = "too_long"
TOO_COMMON = "too_common"
NUMERIC = "numeric"
SIMILAR_PHONE = "similar_phone"


class PasswordRejected(Exception):
    def __init__(self, codes: list[str]):
        super().__init__(", ".join(codes))
        self.codes = codes


def _validators():
    return (
        (TOO_SHORT, pv.MinimumLengthValidator(min_length=settings.CUSTOMER_PASSWORD_MIN_LENGTH)),
        (TOO_COMMON, pv.CommonPasswordValidator()),
        (NUMERIC, pv.NumericPasswordValidator()),
        # Customer n'a ni nom ni e-mail : sans attributs explicites, le
        # validateur ne compare rien. On lui donne le numero, sous ses deux
        # formes (509XXXXXXXX et XXXXXXXX).
        (SIMILAR_PHONE, pv.UserAttributeSimilarityValidator(user_attributes=("phone", "national"))),
    )


class _Subject:
    """Ce que UserAttributeSimilarityValidator lit : les attributs compares
    et, en cas de refus, `_meta.get_field(nom).verbose_name` pour son
    message (que nous n'affichons pas)."""

    _meta = SimpleNamespace(get_field=lambda name: SimpleNamespace(verbose_name=name))

    def __init__(self, phone: str):
        self.phone = phone
        self.national = phone[-8:]


def problems(password: str, phone: str) -> list[str]:
    """Codes des regles non respectees, dans l'ordre. Vide = accepte."""
    password = password or ""
    if len(password) > MAX_LENGTH:
        return [TOO_LONG]
    subject = _Subject(phone or "")
    found = []
    for code, validator in _validators():
        try:
            validator.validate(password, subject)
        except ValidationError:
            found.append(code)
    return found


def check(password: str, phone: str) -> None:
    codes = problems(password, phone)
    if codes:
        raise PasswordRejected(codes)
