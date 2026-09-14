"""Session du client sur le site web.

Un client n'est pas un utilisateur Django : sa session ne passe pas par
django.contrib.auth. Elle ne donne donc aucun acces a la console ni a
l'admin, et une session console ne donne aucun acces au site client.
"""

from __future__ import annotations

import time
from functools import wraps
from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect
from django.urls import reverse

from apps.accounts.models import Customer

CUSTOMER_KEY = "web:customer_id"
LAST_SEEN_KEY = "web:last_seen"


def login_customer(request, customer: Customer) -> None:
    # Nouvel identifiant de session : empeche la fixation de session.
    request.session.cycle_key()
    request.session[CUSTOMER_KEY] = customer.pk
    request.session[LAST_SEEN_KEY] = int(time.time())
    request._web_customer = customer


def logout_customer(request) -> None:
    for key in list(request.session.keys()):
        if key.startswith("web:"):
            del request.session[key]
    request.session.cycle_key()
    request._web_customer = None


def get_customer(request) -> Customer | None:
    """Client de la requete, resolu une seule fois. Toujours comparer a None ici,
    jamais sur request.customer (objet paresseux, pour les gabarits).
    """
    if not hasattr(request, "_web_customer"):
        request._web_customer = current_customer(request)
    return request._web_customer


def current_customer(request) -> Customer | None:
    pk = request.session.get(CUSTOMER_KEY)
    if pk is None:
        return None
    now = int(time.time())
    if now - request.session.get(LAST_SEEN_KEY, 0) > settings.WEB_SESSION_IDLE_SECONDS:
        logout_customer(request)
        return None
    customer = Customer.objects.filter(pk=pk, is_active=True).first()
    if customer is None:
        logout_customer(request)
        return None
    request.session[LAST_SEEN_KEY] = now
    return customer


def customer_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        customer = get_customer(request)
        if customer is None:
            return redirect(f"{reverse('web:login')}?{urlencode({'next': request.get_full_path()})}")
        request.customer = customer  # objet reel pour la vue
        return view(request, *args, **kwargs)

    return wrapper
