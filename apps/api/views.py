"""API publique v1.

Les vues valident, appellent un service et serialisent. Elles ne
modifient aucun modele et ne parlent ni a plopplop ni a Twilio.
"""

from __future__ import annotations

import re

from django.conf import settings
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.pagination import CursorPagination
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import otp
from apps.accounts import services as accounts
from apps.transactions import services as transactions
from apps.transactions.models import Transaction
from apps.transactions.pricing import MIN_NET, AmountTooLarge, AmountTooSmall

from . import services, throttling
from .errors import APIError
from .permissions import IsCustomer
from .serializers import (
    ErrorResponseSerializer,
    MetaSerializer,
    OTPRequestResponseSerializer,
    OTPRequestSerializer,
    OTPVerifySerializer,
    QuoteRequestSerializer,
    QuoteSerializer,
    TokenResponseSerializer,
    TransferCreateSerializer,
    TransferSerializer,
    CustomerSerializer,
)

IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")
ERRORS = {code: OpenApiResponse(ErrorResponseSerializer) for code in (400, 401, 404, 409, 422, 429, 502, 503)}


def _route_error(exc: Exception) -> APIError:
    if isinstance(exc, transactions.MethodDisabled):
        return APIError("METHOD_DISABLED", str(exc))
    if isinstance(exc, AmountTooSmall):
        return APIError("AMOUNT_TOO_SMALL", str(exc))
    if isinstance(exc, AmountTooLarge):
        return APIError("AMOUNT_TOO_LARGE", str(exc))
    return APIError("ROUTE_UNSUPPORTED", str(exc))


ROUTE_ERRORS = (transactions.UnsupportedRoute, AmountTooSmall, AmountTooLarge)


def _saturated_error(exc: transactions.ServiceSaturated) -> APIError:
    """File saturee ou decaissements arretes.

    Ne dit QUE combien de temps attendre, jamais la profondeur de la file
    ni le rang : ce sont des donnees d'exploitation, elles reveleraient le
    volume d'affaires.
    """
    return APIError(
        "SERVICE_SATURATED",
        "Trop de transferts en attente : creation momentanement suspendue.",
        http_status=503,
        extra={"retry_after": exc.retry_after_seconds},
    )


def _limit_error(exc: transactions.LimitExceeded) -> APIError:
    """Plafond cumule atteint. Ne dit que des faits du client lui-meme :
    son plafond, ce qu'il lui reste, quand il se libere. Aucun etat
    interne, aucune information sur les autres clients.
    """
    return APIError(
        "LIMIT_EXCEEDED",
        str(exc),
        http_status=422,
        extra={
            "limit": {
                "window": exc.window.name,
                "cap": str(exc.window.cap),
                "used": str(exc.window.used),
                "remaining": str(exc.window.remaining),
                "frees_at": exc.frees_at.isoformat() if exc.frees_at else None,
            }
        },
    )


# ----------------------------------------------------------------------
# Identification
# ----------------------------------------------------------------------
class OTPRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [
        throttling.OTPRequestPhoneBurstThrottle,
        throttling.OTPRequestPhoneThrottle,
        throttling.OTPIPThrottle,
    ]

    @extend_schema(
        request=OTPRequestSerializer,
        responses={202: OTPRequestResponseSerializer, **ERRORS},
        summary="Envoyer un code de connexion par SMS",
        tags=["Identification"],
    )
    def post(self, request):
        serializer = OTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            phone = accounts.request_code(serializer.validated_data["phone"])
        except otp.OTPRateLimited as exc:
            raise APIError("OTP_RATE_LIMITED", "Trop de demandes de code pour ce numero", http_status=429) from exc
        except otp.OTPInvalidPhone as exc:
            raise APIError("INVALID_PHONE", "Numero refuse par l'operateur SMS") from exc
        except otp.OTPUnavailable as exc:
            raise APIError("OTP_UNAVAILABLE", "Envoi du SMS momentanement impossible", http_status=503) from exc
        return Response(
            {"phone": phone, "expires_in": settings.OTP["CODE_TTL_SECONDS"]}, status=status.HTTP_202_ACCEPTED
        )


class OTPVerifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [throttling.OTPVerifyPhoneThrottle, throttling.OTPIPThrottle]

    @extend_schema(
        request=OTPVerifySerializer,
        responses={200: TokenResponseSerializer, **ERRORS},
        summary="Valider le code et obtenir un jeton d'acces",
        tags=["Identification"],
    )
    def post(self, request):
        serializer = OTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data = serializer.validated_data
            customer, raw, token = accounts.verify_code(
                data["phone"],
                data["code"],
                # La langue retenue sert aux SMS : une tache Celery n'a ni
                # requete ni en-tete.
                language=data.get("language") or request.headers.get("Accept-Language", ""),
            )
        except accounts.InvalidCode as exc:
            raise APIError("OTP_INVALID", "Code invalide ou expire") from exc
        except accounts.CustomerDisabled as exc:
            raise APIError("ACCOUNT_DISABLED", "Compte desactive", http_status=403) from exc
        except otp.OTPRateLimited as exc:
            raise APIError("OTP_RATE_LIMITED", "Trop de tentatives : demander un nouveau code", http_status=429) from exc
        except otp.OTPUnavailable as exc:
            raise APIError("OTP_UNAVAILABLE", "Verification momentanement impossible", http_status=503) from exc
        return Response(
            TokenResponseSerializer(
                {"token": raw, "expires_at": token.expires_at, "customer": customer}
            ).data
        )


class LogoutView(APIView):
    permission_classes = [IsCustomer]

    @extend_schema(request=None, responses={204: None, **ERRORS}, summary="Revoquer le jeton courant", tags=["Identification"])
    def post(self, request):
        accounts.revoke_token(request.auth)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [throttling.CustomerReadThrottle]

    @extend_schema(responses={200: CustomerSerializer, **ERRORS}, summary="Client connecte", tags=["Identification"])
    def get(self, request):
        return Response(CustomerSerializer(request.user).data)


# ----------------------------------------------------------------------
# Catalogue et devis
# ----------------------------------------------------------------------
class MetaView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [throttling.PublicIPThrottle]

    @extend_schema(responses={200: MetaSerializer}, summary="Portefeuilles ouverts et montants limites", tags=["Catalogue"])
    def get(self, request):
        wallets = [
            {
                "code": row["wallet"],
                "label": row["label"],
                "payment_enabled": row["payment_enabled"],
                "payout_enabled": row["payout_enabled"],
            }
            for row in transactions.wallet_availability()
        ]
        data = {
            "currency": "HTG",
            "min_net_amount": MIN_NET,
            "max_net_amount": settings.PRICING["MAX_NET_AMOUNT"],
            "wallets": wallets,
        }
        return Response(MetaSerializer(data).data)


class QuoteView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [throttling.PublicIPThrottle]

    @extend_schema(
        request=QuoteRequestSerializer,
        responses={200: QuoteSerializer, **ERRORS},
        summary="Devis : frais detailles et total debite",
        tags=["Catalogue"],
    )
    def post(self, request):
        serializer = QuoteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            quote = transactions.quote_transfer(**serializer.validated_data)
        except ROUTE_ERRORS as exc:
            raise _route_error(exc) from exc
        return Response(QuoteSerializer(quote).data)


# ----------------------------------------------------------------------
# Transferts
# ----------------------------------------------------------------------
class TransferPagination(CursorPagination):
    page_size = 20
    ordering = ("-created_at", "-id")


class TransferListCreateView(ListAPIView):
    permission_classes = [IsCustomer]
    serializer_class = TransferSerializer
    pagination_class = TransferPagination

    def get_throttles(self):
        if self.request.method == "POST":
            return [throttling.TransferCreateThrottle()]
        return [throttling.CustomerReadThrottle()]

    def get_queryset(self):
        return Transaction.objects.filter(customer=self.request.user)

    @extend_schema(summary="Historique des transferts du client", tags=["Transferts"])
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        request=TransferCreateSerializer,
        parameters=[
            OpenApiParameter(
                "Idempotency-Key",
                str,
                OpenApiParameter.HEADER,
                required=True,
                description="8 a 64 caracteres [A-Za-z0-9_-], unique par demande de transfert",
            )
        ],
        responses={201: TransferSerializer, 200: TransferSerializer, **ERRORS},
        summary="Creer un transfert et lancer le paiement",
        tags=["Transferts"],
    )
    def post(self, request):
        key = request.headers.get("Idempotency-Key", "")
        if not IDEMPOTENCY_KEY.match(key):
            raise APIError("IDEMPOTENCY_KEY_REQUIRED", "En-tete Idempotency-Key requis (8 a 64 caracteres [A-Za-z0-9_-])")

        serializer = TransferCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            outcome = services.create_transfer(
                customer=request.user, idempotency_key=key, **serializer.validated_data
            )
        except services.IdempotencyKeyReused as exc:
            raise APIError("IDEMPOTENCY_KEY_REUSED", str(exc), http_status=422) from exc
        except services.IdempotencyInProgress as exc:
            raise APIError("IDEMPOTENCY_IN_PROGRESS", str(exc), http_status=409) from exc
        except services.QuoteChanged as exc:
            raise APIError(
                "QUOTE_CHANGED",
                str(exc),
                http_status=409,
                extra={"quote": QuoteSerializer(exc.quote).data},
            ) from exc
        except transactions.ServiceSaturated as exc:
            raise _saturated_error(exc) from exc
        except transactions.LimitExceeded as exc:
            raise _limit_error(exc) from exc
        except ROUTE_ERRORS as exc:
            raise _route_error(exc) from exc

        data = TransferSerializer(outcome.transaction).data
        if outcome.payment_failed:
            raise APIError(
                "PAYMENT_PROVIDER_ERROR",
                "Le paiement n'a pas pu etre cree. Aucun montant n'a ete debite.",
                http_status=502,
                extra={"transfer": data},
            )
        headers = {"Idempotent-Replayed": "true"} if outcome.replayed else {}
        return Response(data, status=status.HTTP_200_OK if outcome.replayed else status.HTTP_201_CREATED, headers=headers)


class TransferDetailView(APIView):
    permission_classes = [IsCustomer]
    throttle_classes = [throttling.CustomerReadThrottle]

    @extend_schema(responses={200: TransferSerializer, **ERRORS}, summary="Statut d'un transfert", tags=["Transferts"])
    def get(self, request, reference: str):
        # 404 et non 403 pour la transaction d'un autre client : ne pas
        # confirmer qu'une reference existe.
        txn = Transaction.objects.filter(customer=request.user, reference=reference).first()
        if txn is None:
            raise APIError("NOT_FOUND", "Transfert introuvable", http_status=404)
        return Response(TransferSerializer(txn).data)
