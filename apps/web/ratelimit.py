"""Limites de debit du site web (fenetre fixe, en cache).

L'API applique les siennes via DRF ; celles-ci protegent les memes
operations quand elles passent par le site.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache

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


def client_ip(request) -> str:
    """Meme regle que l'API : X-Forwarded-For seulement derriere des proxies declares."""
    num_proxies = settings.REST_FRAMEWORK.get("NUM_PROXIES") or 0
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if num_proxies and forwarded:
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if len(hops) >= num_proxies:
            return hops[-num_proxies]
    return request.META.get("REMOTE_ADDR", "")
