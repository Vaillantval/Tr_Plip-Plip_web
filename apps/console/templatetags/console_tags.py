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
