from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django import template

from apps.transactions.states import State

register = template.Library()

EMPTY = "—"


@register.filter
def duration(seconds):
    """Duree compacte : 45 s, 3 min 20 s, 2 h 05 min, 3 j 04 h."""
    if seconds is None or seconds == "":
        return EMPTY
    s = max(int(seconds), 0)
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h} h {m:02d} min"
    d, h = divmod(h, 24)
    return f"{d} j {h:02d} h"


@register.filter
def hours_duration(hours):
    if hours is None or hours == "":
        return EMPTY
    return duration(Decimal(hours) * 3600)


@register.filter
def htg(value):
    """Montant a deux decimales, milliers separes par une espace fine."""
    if value is None or value == "":
        return EMPTY
    q = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{q:,.2f}".replace(",", " ")


@register.filter
def abs_htg(value):
    if value is None or value == "":
        return EMPTY
    return htg(abs(Decimal(value)))


@register.filter
def percent(ratio):
    if ratio is None or ratio == "":
        return EMPTY
    return f"{Decimal(ratio) * 100:.0f} %"


@register.filter
def percent_rate(rate):
    """Taux decimal -> pourcentage lisible : 0.025 -> « 2,5 % »."""
    if rate is None or rate == "":
        return EMPTY
    value = (Decimal(rate) * 100).quantize(Decimal("0.01")).normalize()
    return f"{value:f}".replace(".", ",") + " %"


@register.filter
def add_htg(a, b):
    return Decimal(a or 0) + Decimal(b or 0)


@register.filter
def state_label(value):
    try:
        return State(value).label
    except ValueError:
        return value or EMPTY


#: Icones du menu : traces SVG (viewBox 24, trait), sans bibliotheque.
ICONS = {
    "home": "M3 11l9-7 9 7M5 10v10h5v-6h4v6h5V10",
    "queue": "M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0",
    "swap": "M7 7h13l-4-4M17 17H4l4 4",
    "alert": "M12 9v4M12 17h.01M10.3 3.9 2.4 18a2 2 0 0 0 1.7 3h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z",
    "chat": "M21 12a8 8 0 0 1-11.6 7.1L4 20l1.1-4.4A8 8 0 1 1 21 12z",
    "wallet": "M3 7h15a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V7zm0 0V6a2 2 0 0 1 2-2h11M16 14h.01",
    "sliders": "M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1M15 4v4M9 10v4M17 16v4",
}

#: Menu : (groupe, [(nom d'URL, libelle, icone, ecrans actifs, pastille)]).
NAV = (
    (
        "Exploitation",
        (
            ("dashboard", "Tableau de bord", "home", {"dashboard"}, None),
            ("queue", "File de décaissement", "queue", {"queue"}, None),
            ("transactions", "Transactions", "swap", {"transactions", "transaction_detail"}, None),
            ("exceptions", "Exceptions", "alert", {"exceptions"}, "exceptions"),
            ("claims", "Réclamations", "chat", {"claims", "claim_detail"}, "claims"),
        ),
    ),
    (
        "Argent",
        (
            ("treasury", "Trésorerie", "wallet", {"treasury"}, None),
            ("methods", "Méthodes et tarifs", "sliders", {"methods"}, None),
        ),
    ),
)


@register.inclusion_tag("console/partials/nav.html", takes_context=True)
def console_nav(context):
    """Menu lateral, avec les pastilles « a traiter ».

    Les compteurs sont calcules une fois par page, meme si le menu est
    rendu deux fois (bureau et telephone) : ils sont gardes dans le
    contexte de rendu.
    """
    from django.urls import reverse

    from apps.console import metrics

    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    current = match.url_name if match else ""
    cache = context.render_context.get("console_nav_counts")
    if cache is None:
        cache = {"exceptions": metrics.exceptions_to_handle(), "claims": metrics.open_claims()}
        context.render_context["console_nav_counts"] = cache
    groups = []
    for title, entries in NAV:
        items = []
        for name, label, icon, active, badge in entries:
            items.append(
                {
                    "url": reverse(f"console:{name}"),
                    "label": label,
                    "icon": ICONS[icon],
                    "active": current in active,
                    # Pas de pastille « 0 » : elle attirerait l'oeil pour rien.
                    "badge": cache.get(badge) or None,
                    "badge_kind": badge,
                }
            )
        groups.append({"title": title, "items": items})
    return {"groups": groups}


@register.inclusion_tag("console/partials/worker_alert.html", takes_context=True)
def payout_worker_alert(context):
    """Bandeau d'incident : worker de decaissement a l'arret, file non vide.

    Opérateurs connectes uniquement : jamais sur la page de connexion.
    """
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"worker": None}
    from apps.transactions.queue import payout_worker_status

    return {"worker": payout_worker_status()}


@register.simple_tag
def environment_name():
    """Nom de l'environnement Railway (« production »...), vide en local.

    Affiche en haut de la console pour qu'on ne confonde jamais un
    environnement de test avec la production.
    """
    import os

    return os.environ.get("RAILWAY_ENVIRONMENT_NAME", "")
