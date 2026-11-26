"""Limites de debit du site web (fenetre fixe, en cache).

L'API applique les siennes via DRF ; celles-ci protegent les memes
operations quand elles passent par le site.
"""

from __future__ import annotations

from django.core.cache import cache

from apps.accounts.ip import client_ip as _client_ip

#: (nombre, fenetre en secondes)
LIMITS = {
    "otp_request_phone_burst": (1, 60),
    "otp_request_phone": (5, 3600),
    "otp_request_ip": (30, 3600),
    "otp_verify_phone": (10, 3600),
    "quote_ip": (120, 60),
    "transfer_create_customer": (20, 3600),
    # Une reclamation par transfert suffit : au-dela de trois par heure,
    # c'est du remplissage, et chacune coute du temps d'operateur.
    "claim_create_customer": (3, 3600),
    "claim_message_customer": (10, 3600),
}


def allow(scope: str, ident) -> bool:
    limit, window = LIMITS[scope]
    key = f"web:rl:{scope}:{ident}"
    if cache.add(key, 1, window):
        return True
    try:
        count = cache.incr(key)
    except ValueError:  # cle expiree entre add et incr
        cache.set(key, 1, window)
        return True
    return count <= limit


#: Meme regle que l'API : voir apps.accounts.ip.
client_ip = _client_ip
