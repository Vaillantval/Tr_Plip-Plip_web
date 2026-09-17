from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from rest_framework import serializers

from apps.accounts.phone import InvalidPhone, normalize
from apps.transactions.models import Wallet

from .status import LABELS, payment_instructions, public_status, public_wait

MONEY = {"max_digits": 12, "decimal_places": 2}


class PhoneField(serializers.CharField):
    def __init__(self, **kwargs):
        kwargs.setdefault("max_length", 24)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not value and not self.required:
            return ""
        try:
            return normalize(value)
        except InvalidPhone as exc:
            raise serializers.ValidationError(str(exc), code="invalid_phone") from exc


# ----------------------------------------------------------------------
# Identification
# ----------------------------------------------------------------------
class OTPRequestSerializer(serializers.Serializer):
    phone = PhoneField()


class OTPRequestResponseSerializer(serializers.Serializer):
    phone = serializers.CharField()
    expires_in = serializers.IntegerField(help_text="Duree de validite du code, en secondes")


class OTPVerifySerializer(serializers.Serializer):
    phone = PhoneField()
    code = serializers.RegexField(r"^\d{4,10}$", error_messages={"invalid": "Code numerique attendu"})
    language = serializers.ChoiceField(
        choices=[code for code, _ in settings.LANGUAGES],
        required=False,
        help_text=(
            "Langue du client, retenue pour les notifications SMS. "
            "A defaut, l'en-tete Accept-Language est utilise."
        ),
    )


class CustomerSerializer(serializers.Serializer):
    phone = serializers.CharField()
    created_at = serializers.DateTimeField()


class TokenResponseSerializer(serializers.Serializer):
    token = serializers.CharField(help_text="A envoyer dans `Authorization: Bearer <token>`")
    expires_at = serializers.DateTimeField()
    customer = CustomerSerializer()


# ----------------------------------------------------------------------
# Catalogue et devis
# ----------------------------------------------------------------------
class WalletSerializer(serializers.Serializer):
    code = serializers.CharField()
    label = serializers.CharField()
    payment_enabled = serializers.BooleanField(help_text="Utilisable comme source")
    payout_enabled = serializers.BooleanField(help_text="Utilisable comme destination")


class MetaSerializer(serializers.Serializer):
    currency = serializers.CharField()
    min_net_amount = serializers.DecimalField(**MONEY)
    max_net_amount = serializers.DecimalField(**MONEY)
    wallets = WalletSerializer(many=True)


class QuoteRequestSerializer(serializers.Serializer):
    source_wallet = serializers.ChoiceField(choices=Wallet.choices)
    destination_wallet = serializers.ChoiceField(choices=Wallet.choices)
    net_amount = serializers.DecimalField(
        **MONEY, min_value=Decimal("0.01"), help_text="Montant recu par le beneficiaire"
    )


class QuoteSerializer(serializers.Serializer):
    source_wallet = serializers.CharField()
    destination_wallet = serializers.CharField()
    currency = serializers.CharField(default="HTG")
    net_amount = serializers.DecimalField(**MONEY, help_text="Montant recu par le beneficiaire")
    fee_in = serializers.DecimalField(**MONEY, help_text="Frais operateur a l'encaissement")
    fee_out = serializers.DecimalField(**MONEY, help_text="Frais operateur au decaissement")
    fee_platform = serializers.DecimalField(**MONEY, help_text="Commission Plip-Plip")
    total_fees = serializers.DecimalField(**MONEY)
    total_charged = serializers.DecimalField(**MONEY, help_text="Montant debite au payeur")


# ----------------------------------------------------------------------
# Transferts
# ----------------------------------------------------------------------
class TransferCreateSerializer(QuoteRequestSerializer):
    recipient_phone = PhoneField()
    sender_phone = PhoneField(
        required=False,
        allow_blank=True,
        help_text="Numero MonCash du payeur : declenche une demande USSD au lieu d'une redirection",
    )
    expected_total = serializers.DecimalField(
        **MONEY,
        help_text="total_charged du devis accepte par le client. Refus 409 s'il ne correspond plus.",
    )


class PaymentInstructionsSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(
        choices=["redirect", "ussd", "unavailable", "none"],
        help_text=(
            "redirect : ouvrir redirect_url. ussd : valider la demande recue sur le telephone. "
            "unavailable : lien de paiement non obtenu, ne pas recreer le transfert tout de suite, "
            "consulter le statut. none : plus de paiement attendu."
        ),
    )
    redirect_url = serializers.CharField(allow_null=True)
    expires_at = serializers.DateTimeField(allow_null=True)


class EstimatedWaitSerializer(serializers.Serializer):
    available = serializers.BooleanField(
        help_text="false : transfert en cours, delai non garanti. Ne pas afficher de duree."
    )
    min_minutes = serializers.IntegerField(allow_null=True, help_text="0 : « moins de max_minutes minutes »")
    max_minutes = serializers.IntegerField(allow_null=True)


class TransferSerializer(serializers.Serializer):
    reference = serializers.CharField()
    status = serializers.ChoiceField(choices=list(LABELS))
    status_label = serializers.CharField()
    source_wallet = serializers.CharField()
    destination_wallet = serializers.CharField()
    recipient_phone = serializers.CharField()
    amounts = QuoteSerializer()
    payment = PaymentInstructionsSerializer()
    estimated_wait = EstimatedWaitSerializer(
        allow_null=True, help_text="Delai estime avant reception, seulement pendant le statut in_progress."
    )
    created_at = serializers.DateTimeField()
    delivered_at = serializers.DateTimeField(allow_null=True)

    def to_representation(self, txn):
        status = public_status(txn.state)
        return super().to_representation(
            {
                "reference": txn.reference,
                "status": status,
                "status_label": LABELS[status],
                "source_wallet": txn.source_wallet,
                "destination_wallet": txn.destination_wallet,
                "recipient_phone": txn.recipient_phone,
                "amounts": {
                    "source_wallet": txn.source_wallet,
                    "destination_wallet": txn.destination_wallet,
                    "currency": "HTG",
                    "net_amount": txn.net_amount,
                    "fee_in": txn.fee_in,
                    "fee_out": txn.fee_out,
                    "fee_platform": txn.fee_platform,
                    "total_fees": txn.fee_in + txn.fee_out + txn.fee_platform,
                    "total_charged": txn.total_charged,
                },
                "payment": payment_instructions(txn, status),
                "estimated_wait": public_wait(txn, self.context),
                "created_at": txn.created_at,
                "delivered_at": txn.payout_completed_at,
            }
        )


class ErrorSerializer(serializers.Serializer):
    code = serializers.CharField()
    message = serializers.CharField()
    fields = serializers.DictField(required=False)


class ErrorResponseSerializer(serializers.Serializer):
    error = ErrorSerializer()
