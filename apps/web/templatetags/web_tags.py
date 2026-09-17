from __future__ import annotations

from django import template

from apps.api.status import public_status

from ..labels import STATUS_LABELS, format_htg, wallet_label

register = template.Library()


@register.filter
def htg(value):
    return format_htg(value)


@register.filter
def wallet(code):
    return wallet_label(code)


@register.filter
def status_of(txn):
    return public_status(txn.state)


@register.filter
def status_label(status):
    return STATUS_LABELS.get(status, status)
