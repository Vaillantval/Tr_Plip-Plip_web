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


#: Numero haitien bien forme, mobile ou non. Sert a distinguer « vous avez
#: mal tape » de « ce numero ne peut pas recevoir d'argent » : les deux
#: n'appellent pas la meme reaction du client.
_WELL_FORMED = re.compile(r"^509\d{8}$")


class InvalidPhone(ValueError):
    pass


class NotAMobileNumber(InvalidPhone):
    """Numero correct, mais incapable de porter un portefeuille.

    Le distinguer compte : un client a qui l'on dit « numero invalide »
    devant un fixe parfaitement ecrit le ressaisit a l'identique, et
    recommence. Il faut lui dire que ce numero-la ne peut pas recevoir
    d'argent.
    """


def normalize(value: str) -> str:
    raw = _SEPARATORS.sub("", str(value or ""))
    if raw.startswith("+"):
        raw = raw[1:]
    elif raw.startswith("00"):
        raw = raw[2:]
    if len(raw) == 8:
        raw = COUNTRY_CODE + raw
    if not _CANONICAL.match(raw):
        if _WELL_FORMED.match(raw):
            raise NotAMobileNumber(
                "Ce numero ne peut pas recevoir d'argent : MonCash et NatCash fonctionnent "
                "uniquement sur des lignes mobiles"
            )
        raise InvalidPhone("Numero haitien invalide : 8 chiffres, mobile (3, 4 ou 5), avec ou sans +509")
    return raw


def to_e164(canonical: str) -> str:
    return f"+{canonical}"
