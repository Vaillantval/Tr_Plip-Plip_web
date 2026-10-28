"""Verification des appels de Twilio vers nous (X-Twilio-Signature).

Sans elle, n'importe qui pourrait poster « echec » sur notre URL de statut
et declencher un SMS de repli a chaque appel : un cout a la main de
n'importe quel inconnu.

Algorithme documente par Twilio : l'URL complete appelee, suivie de chaque
parametre POST trie par nom (nom puis valeur, sans separateur), signee en
HMAC-SHA1 avec l'AUTH_TOKEN du compte, encodee en base64.
"""

from __future__ import annotations

import base64
import hashlib
import hmac


def expected_signature(url: str, params: dict[str, str], auth_token: str) -> str:
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))
    digest = hmac.new(auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def is_valid(url: str, params: dict[str, str], signature: str, auth_token: str) -> bool:
    if not (signature and auth_token):
        return False
    return hmac.compare_digest(expected_signature(url, params, auth_token), signature)
