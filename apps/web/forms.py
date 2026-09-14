from __future__ import annotations

from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.phone import InvalidPhone, normalize

from .labels import wallet_label


class PhoneField(forms.CharField):
    default_error_messages = {
        "invalid_phone": _("Numéro invalide : 8 chiffres d'un mobile haïtien, avec ou sans +509."),
    }

    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 24)
        super().__init__(**kwargs)

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return ""
        try:
            return normalize(value)
        except InvalidPhone as exc:
            raise forms.ValidationError(self.error_messages["invalid_phone"], code="invalid_phone") from exc


class PhoneForm(forms.Form):
    phone = PhoneField(label=_("Votre numéro de téléphone"))


class CodeForm(forms.Form):
    code = forms.RegexField(
        regex=r"^\d{4,10}$",
        label=_("Code reçu par SMS"),
        error_messages={"invalid": _("Saisissez les chiffres du code reçu par SMS.")},
    )


class TransferForm(forms.Form):
    """Formulaire d'envoi. Les portefeuilles proposes sont ceux ouverts."""

    source_wallet = forms.ChoiceField(label=_("J'envoie depuis"))
    destination_wallet = forms.ChoiceField(label=_("Le bénéficiaire reçoit sur"))
    recipient_phone = PhoneField(label=_("Numéro du bénéficiaire"))
    net_amount = forms.DecimalField(
        label=_("Montant reçu par le bénéficiaire (HTG)"),
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
    )
    sender_phone = PhoneField(
        label=_("Numéro MonCash qui paie (facultatif)"),
        required=False,
        help_text=_("Renseigné, vous recevez la demande de paiement directement sur ce téléphone."),
    )

    def __init__(self, *args, wallets, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["source_wallet"].choices = [
            (w["wallet"], wallet_label(w["wallet"])) for w in wallets if w["payment_enabled"]
        ]
        self.fields["destination_wallet"].choices = [
            (w["wallet"], wallet_label(w["wallet"])) for w in wallets if w["payout_enabled"]
        ]

    def clean(self):
        data = super().clean()
        if data.get("source_wallet") and data.get("source_wallet") == data.get("destination_wallet"):
            self.add_error("destination_wallet", _("Choisissez un portefeuille différent de celui d'envoi."))
        return data
