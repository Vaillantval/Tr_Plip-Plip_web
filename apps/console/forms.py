from __future__ import annotations

from decimal import Decimal

from django import forms

from apps.transactions.services import REFUND_REASON_MAX_LENGTH, TRANSFER_REFERENCE_MAX_LENGTH


class RefundForm(forms.Form):
    """Constat d'un remboursement deja effectue hors systeme."""

    reason = forms.CharField(label="Motif", max_length=REFUND_REASON_MAX_LENGTH)
    transfer_reference = forms.CharField(
        label="Reference du transfert manuel deja effectue",
        max_length=TRANSFER_REFERENCE_MAX_LENGTH,
    )


class TopupForm(forms.Form):
    amount = forms.DecimalField(
        label="Montant (HTG)", max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    reference = forms.CharField(label="Reference du rechargement plopplop", max_length=64)
