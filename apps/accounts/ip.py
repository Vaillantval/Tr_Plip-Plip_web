"""Adresse IP du client, une seule regle pour tout le projet.

X-Forwarded-For n'est lu que derriere des proxies declares
(REST_FRAMEWORK["NUM_PROXIES"]), et depuis la DROITE : le premier saut
est ecrit par le client lui-meme, n'importe qui peut y mettre n'importe
quelle adresse. S'y fier, c'est laisser un attaquant choisir l'IP qu'il
contourne (limites, verrous) et celle qu'il fait accuser (audit).
"""

from __future__ import annotations

from django.conf import settings


def client_ip(request) -> str:
    num_proxies = settings.REST_FRAMEWORK.get("NUM_PROXIES") or 0
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if num_proxies and forwarded:
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if len(hops) >= num_proxies:
            return hops[-num_proxies]
    return request.META.get("REMOTE_ADDR", "")
