from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django import template

from apps.api.status import public_status

from ..labels import STATUS_LABELS, wallet_label

register = template.Library()


@register.filter
def htg(value):
    """1090 -> « 1 090,00 » (espace fine insecable, virgule decimale)."""
    if value is None or value == "":
        return "—"
    q = Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{q:,.2f}".replace(",", " ").replace(".", ",")


@register.filter
def wallet(code):
    return wallet_label(code)


@register.filter
def status_of(txn):
    return public_status(txn.state)


@register.filter
def status_label(status):
    return STATUS_LABELS.get(status, status)
