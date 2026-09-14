"""Numeros de telephone haitiens.

Format canonique stocke partout : 509 suivi de 8 chiffres, sans signe
(ex. 50937123456). C'est le format envoye a plopplop ; Twilio recoit la
forme E.164 (+50937123456).
"""

from __future__ import annotations

import re

COUNTRY_CODE = "509"
#: Numeros mobiles : les portefeuilles MonCash et NatCash sont rattaches a
#: des lignes mobiles, dont le premier chiffre local est 3, 4 ou 5. Les
#: lignes fixes (2...) sont refusees.
_CANONICAL = re.compile(r"^509[345]\d{7}$")
_SEPARATORS = re.compile(r"[\s\-.()]")


class InvalidPhone(ValueError):
    pass


def normalize(value: str) -> str:
    raw = _SEPARATORS.sub("", str(value or ""))
    if raw.startswith("+"):
        raw = raw[1:]
    elif raw.startswith("00"):
        raw = raw[2:]
    if len(raw) == 8:
        raw = COUNTRY_CODE + raw
    if not _CANONICAL.match(raw):
        raise InvalidPhone("Numero haitien invalide : 8 chiffres, mobile (3, 4 ou 5), avec ou sans +509")
    return raw


def to_e164(canonical: str) -> str:
    return f"+{canonical}"
