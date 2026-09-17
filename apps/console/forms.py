from __future__ import annotations

from decimal import Decimal

from django import forms
from django.contrib.auth.forms import AuthenticationForm, UsernameField

from apps.transactions.services import REFUND_REASON_MAX_LENGTH, TRANSFER_REFERENCE_MAX_LENGTH


class RefundForm(forms.Form):
    """Constat d'un remboursement deja effectue hors systeme."""

    reason = forms.CharField(label="Motif", max_length=REFUND_REASON_MAX_LENGTH)
    transfer_reference = forms.CharField(
        label="Reference du transfert manuel deja effectue",
        max_length=TRANSFER_REFERENCE_MAX_LENGTH,
    )
    # Encaissement bloque sans montant communique : montant verifie et rendu.
    refunded_amount = forms.DecimalField(
        label="Montant rendu (HTG)", required=False, max_digits=12, decimal_places=2, min_value=Decimal("0.01")
    )


def _percent_field(label: str) -> forms.DecimalField:
    return forms.DecimalField(
        label=label, max_digits=5, decimal_places=2, min_value=Decimal("0"), max_value=Decimal("49.99")
    )


#: (champ, libelle, sortie uniquement)
RATE_INPUTS = (
    ("payment_fee_rate", "Frais client a l'envoi", False),
    ("payout_fee_rate", "Frais client a la reception", True),
    ("payment_cost_rate", "Cout plopplop a l'encaissement", False),
    ("payout_cost_rate", "Cout plopplop au retrait", True),
)


def pricing_field_name(wallet: str, field: str) -> str:
    return f"{wallet}__{field}"


class PricingForm(forms.Form):
    """Tarifs saisis en POURCENTAGES (2,5 = 2,5 %), stockes en taux decimaux."""

    platform_fee_rate = _percent_field("Commission Plip-Plip")

    def __init__(self, *args, wallets, **kwargs):
        super().__init__(*args, **kwargs)
        self.wallets = wallets
        for wallet in wallets:
            for field, label, payout_only in RATE_INPUTS:
                if payout_only and not wallet["payout_capable"]:
                    continue
                self.fields[pricing_field_name(wallet["wallet"], field)] = _percent_field(f"{wallet['label']} — {label}")

    def rates(self) -> tuple[Decimal, dict]:
        data = self.cleaned_data
        wallet_rates = {}
        for wallet in self.wallets:
            wallet_rates[wallet["wallet"]] = {
                field: data.get(pricing_field_name(wallet["wallet"], field), Decimal("0")) / 100
                for field, _, _ in RATE_INPUTS
            }
        return data["platform_fee_rate"] / 100, wallet_rates


class ReleaseForm(forms.Form):
    """Deblocage d'un encaissement apres verification chez plopplop."""

    verified_amount = forms.DecimalField(
        label="Montant verifie chez plopplop (HTG)", max_digits=12, decimal_places=2, min_value=Decimal("0.01")
    )
    reason = forms.CharField(label="Motif / source de la verification", max_length=REFUND_REASON_MAX_LENGTH)


class TopupForm(forms.Form):
    amount = forms.DecimalField(
        label="Montant (HTG)", max_digits=14, decimal_places=2, min_value=Decimal("0.01")
    )
    reference = forms.CharField(label="Reference du rechargement plopplop", max_length=64)


class LimitsForm(forms.Form):
    """Plafonds cumules par client, en HTG."""

    daily_cap = forms.DecimalField(
        label="Plafond par jour glissant (HTG)", max_digits=12, decimal_places=2, min_value=Decimal("1")
    )
    monthly_cap = forms.DecimalField(
        label="Plafond sur 30 jours glissants (HTG)", max_digits=12, decimal_places=2, min_value=Decimal("1")
    )


class ConsoleLoginForm(AuthenticationForm):
    """Connexion a la console.

    L'identifiant est une adresse e-mail (info@plip.ht par defaut), d'ou
    le libelle et le clavier adaptes. Le message d'echec ne distingue
    jamais « compte inconnu » de « mot de passe faux » : il ne doit pas
    servir a decouvrir les comptes d'exploitation.
    """

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": "Identifiant ou mot de passe incorrect.",
        "inactive": "Ce compte est desactive.",
    }

    #: Habillage porte par le widget : un champ non style ne doit jamais
    #: apparaitre, meme le temps d'un chargement ou sans JavaScript.
    INPUT_CLASS = (
        "w-full rounded border border-slate-300 px-3 py-2 "
        "focus:border-slate-900 focus:outline-none focus:ring-1 focus:ring-slate-900"
    )

    username = UsernameField(
        label="Identifiant",
        widget=forms.TextInput(
            attrs={
                "class": INPUT_CLASS,
                "autofocus": True,
                "autocomplete": "username",
                "inputmode": "email",
                "spellcheck": "false",
            }
        ),
    )
    password = forms.CharField(
        label="Mot de passe",
        strip=False,
        widget=forms.PasswordInput(attrs={"class": INPUT_CLASS, "autocomplete": "current-password"}),
    )
