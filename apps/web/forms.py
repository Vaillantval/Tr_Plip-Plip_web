from __future__ import annotations

from decimal import Decimal

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.phone import InvalidPhone, NotAMobileNumber, normalize
from apps.claims import services as claims
from apps.claims.models import Reason

from .labels import wallet_label


class PhoneField(forms.CharField):
    default_error_messages = {
        "invalid_phone": _("Numéro invalide : 8 chiffres d'un mobile haïtien, avec ou sans +509."),
        # Message distinct, et ce n'est pas un detail : devant un fixe
        # parfaitement ecrit, « numero invalide » pousse le client a le
        # ressaisir a l'identique, puis a recommencer. Il faut lui dire
        # que ce numero-la ne peut pas recevoir d'argent.
        "not_mobile": _(
            "Ce numéro ne peut pas recevoir d'argent : MonCash et NatCash "
            "fonctionnent uniquement sur des lignes mobiles."
        ),
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
        except NotAMobileNumber as exc:
            raise forms.ValidationError(self.error_messages["not_mobile"], code="not_mobile") from exc
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


class ClaimForm(forms.Form):
    """Ouverture d'une reclamation.

    Les bornes de longueur viennent du service : le formulaire ne decide
    de rien, il presente. Une vue DRF appliquera les memes regles sans
    passer par ici.
    """

    reason = forms.ChoiceField(label=_("Que s'est-il passé ?"), choices=[], widget=forms.RadioSelect)
    body = forms.CharField(
        label=_("Expliquez en quelques mots"),
        widget=forms.Textarea(attrs={"rows": 5}),
        min_length=claims.BODY_MIN_LENGTH,
        max_length=claims.BODY_MAX_LENGTH,
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["reason"].choices = Reason.choices


class ClaimMessageForm(forms.Form):
    body = forms.CharField(
        label=_("Ajouter un message"),
        widget=forms.Textarea(attrs={"rows": 3}),
        min_length=claims.BODY_MIN_LENGTH,
        max_length=claims.BODY_MAX_LENGTH,
    )


class RecipientCheckForm(forms.Form):
    """Ressaisie du numero du beneficiaire, avant de payer.

    Un chiffre faux envoie l'argent a un inconnu, et rien ne le fait
    revenir : le decaissement est definitif. La ressaisie se fait sur la
    page de confirmation, ou le numero n'est plus affiche qu'en partie,
    pour que le client le retape au lieu de le recopier.
    """

    recipient_phone_confirm = PhoneField(
        label=_("Retapez le numéro du bénéficiaire"),
        widget=forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "off"}),
    )

    def __init__(self, *args, expected: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.expected = expected

    def clean_recipient_phone_confirm(self):
        value = self.cleaned_data["recipient_phone_confirm"]
        if value != self.expected:
            raise forms.ValidationError(
                _("Les deux numéros ne correspondent pas. Retapez-le, ou modifiez l'envoi si le premier était faux."),
                code="mismatch",
            )
        return value


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
