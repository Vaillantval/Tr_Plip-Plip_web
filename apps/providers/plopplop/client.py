"""Client HTTP plopplop.

Seul module autorise a parler a plopplop. Tout le reste du code passe
par cette interface, ce qui permettra de brancher un second PSP plus
tard sans toucher au moteur transactionnel.

Regles de securite appliquees ici :
  - le client_secret ne quitte jamais le serveur ;
  - toute ecriture qui part en timeout leve PlopPlopIndeterminate,
    jamais une erreur banale : l'appelant doit verifier avant de
    conclure quoi que ce soit ;
  - le montant signe et le montant envoye sont produits par la meme
    fonction de formatage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import requests

from .exceptions import PlopPlopError, PlopPlopIndeterminate, from_response
from .signing import sign_withdrawal

logger = logging.getLogger(__name__)

# Moyens acceptes en ENTREE (api/paiement-marchand)
PAYMENT_METHODS = ("moncash", "moncash_ussd", "kashpaw", "natcash", "carte", "all")
# Moyens acceptes en SORTIE (api/withdraw/marchand) -- volontairement plus etroit
WITHDRAWAL_METHODS = ("moncash", "natcash")

WRITE_ENDPOINTS = frozenset({"api/paiement-marchand", "api/withdraw/marchand"})


@dataclass(frozen=True)
class PaymentIntent:
    transaction_id: str
    reference: str
    redirect_url: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_ussd(self) -> bool:
        return self.redirect_url is None


@dataclass(frozen=True)
class PaymentStatus:
    reference: str
    transaction_id: str | None
    confirmed: bool
    amount: Decimal | None
    method: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class WithdrawalResult:
    transaction_id: str
    api_reference: str | None
    reference: str
    amount: Decimal
    fee: Decimal
    total: Decimal
    balance_after: Decimal | None
    status: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True)
class WithdrawalStatus:
    reference: str
    status: str  # pending | failed | success | rembourse
    transaction_id: str | None
    amount: Decimal | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_settled(self) -> bool:
        return self.status in ("success", "failed", "rembourse", "rembours\u00e9")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


class PlopPlopClient:
    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self._client_secret = client_secret
        self.timeout = timeout
        self._session = session or requests.Session()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _post(self, endpoint: str, body: dict, *, token: str | None = None) -> dict:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = self._session.post(url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            if endpoint in WRITE_ENDPOINTS:
                raise PlopPlopIndeterminate(
                    f"Aucune reponse de {endpoint} : etat inconnu, verification requise"
                ) from exc
            raise PlopPlopError(f"Echec reseau sur {endpoint}: {exc}") from exc

        if response.status_code >= 500 and endpoint in WRITE_ENDPOINTS:
            raise PlopPlopIndeterminate(
                f"{endpoint} a repondu {response.status_code} : etat inconnu, verification requise",
                status=response.status_code,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise PlopPlopError(
                f"Reponse non-JSON de {endpoint} (HTTP {response.status_code})",
                status=response.status_code,
            ) from exc

        ok_flag = payload.get("success", payload.get("status"))
        if response.status_code >= 400 or ok_flag is False:
            raise from_response(response.status_code, payload)

        return payload

    # ------------------------------------------------------------------
    # Encaissement
    # ------------------------------------------------------------------
    def create_payment(
        self,
        *,
        reference: str,
        amount: Decimal,
        method: str,
        phone_number: str | None = None,
    ) -> PaymentIntent:
        """api/paiement-marchand -- cree la jambe entrante.

        `amount` est le montant TOTAL debite au payeur (net + frais),
        pas le montant destine au beneficiaire.
        """
        if method not in PAYMENT_METHODS:
            raise ValueError(f"Moyen de paiement inconnu : {method}")
        if method == "moncash_ussd" and not phone_number:
            raise ValueError("phone_number est obligatoire pour moncash_ussd")
        if amount < Decimal("20"):
            raise ValueError("Le montant minimum accepte par plopplop est de 20 HTG")

        body: dict[str, Any] = {
            "client_id": self.client_id,
            "refference_id": reference,  # orthographe imposee par l'API
            "montant": float(amount),
            "payment_method": method,
        }
        if phone_number:
            body["phone_number"] = phone_number

        payload = self._post("api/paiement-marchand", body)
        return PaymentIntent(
            transaction_id=str(payload.get("transaction_id")),
            reference=reference,
            redirect_url=payload.get("url") or None,
            raw=payload,
        )

    def payment_status(self, reference: str) -> PaymentStatus:
        """api/paiement-verify -- trans_status vaut 'no' ou 'ok'.

        Il n'existe PAS d'etat d'echec cote paiement : un paiement
        jamais complete reste 'no' indefiniment. L'expiration est donc
        une decision locale (voir PAYMENT_EXPIRY dans les settings).
        """
        payload = self._post(
            "api/paiement-verify",
            {"client_id": self.client_id, "refference_id": reference},
        )
        return PaymentStatus(
            reference=reference,
            transaction_id=(str(payload["id_transaction"]) if payload.get("id_transaction") else None),
            confirmed=payload.get("trans_status") == "ok",
            amount=_dec(payload.get("montant")),
            method=payload.get("method"),
            raw=payload,
        )

    # ------------------------------------------------------------------
    # Decaissement -- 3 etapes
    # ------------------------------------------------------------------
    def authenticate(self) -> str:
        """Etape 1. Le jeton est court-vecu : ne jamais le mettre en cache
        au dela d'une operation. La doc se contredit sur sa duree
        (texte ~60 s, champ expires_in 300) -- on ne s'y fie pas.
        """
        payload = self._post(
            "api/auth/marchand",
            {"client_id": self.client_id, "client_secret": self._client_secret},
        )
        return payload["token"]

    def withdrawal_token(
        self,
        *,
        auth_token: str,
        amount: Decimal,
        method: str,
        recipient: str,
        reference: str,
    ) -> tuple[str, str]:
        """Etape 2. Retourne (withdrawal_token, montant_formate).

        Le montant formate rendu ici doit etre reutilise a l'identique
        a l'etape 3, sinon PARAMETER_MISMATCH.
        """
        if method not in WITHDRAWAL_METHODS:
            raise ValueError(f"Retrait impossible vers {method}")

        signature, timestamp, formatted = sign_withdrawal(
            amount=amount,
            method=method,
            recipient=recipient,
            reference=reference,
            client_secret=self._client_secret,
        )
        payload = self._post(
            "api/auth/marchand/withdrawal-token",
            {
                "amount": float(formatted),
                "method": method,
                "recipient": recipient,
                "reference": reference,
                "timestamp": timestamp,
                "withdrawal_signature": signature,
            },
            token=auth_token,
        )
        return payload["withdrawal_token"], formatted

    def execute_withdrawal(
        self,
        *,
        withdrawal_token: str,
        amount: str,
        method: str,
        recipient: str,
        reference: str,
    ) -> WithdrawalResult:
        """Etape 3. Le jeton est a usage unique et expire en 2 minutes.

        En cas de PlopPlopIndeterminate, ne JAMAIS rappeler cette
        methode : passer par withdrawal_status(reference).
        """
        payload = self._post(
            "api/withdraw/marchand",
            {
                "amount": float(amount),
                "method": method,
                "recipient": recipient,
                "reference": reference,
            },
            token=withdrawal_token,
        )
        data = payload.get("data", {})
        return WithdrawalResult(
            transaction_id=str(data.get("transaction_id")),
            api_reference=(str(data["api_reference"]) if data.get("api_reference") else None),
            reference=reference,
            amount=_dec(data.get("amount")) or Decimal("0"),
            fee=_dec(data.get("fee")) or Decimal("0"),
            total=_dec(data.get("total")) or Decimal("0"),
            balance_after=_dec(data.get("balance_after")),
            status=data.get("status", "unknown"),
            raw=payload,
        )

    def withdrawal_status(self, *, auth_token: str, reference: str) -> WithdrawalStatus:
        """api/withdraw/marchand/verify -- le seul appel autorise apres
        une issue indeterminee. Un 404 signifie qu'aucun retrait ne
        porte cette reference : le decaissement n'est jamais parti.
        """
        payload = self._post(
            "api/withdraw/marchand/verify",
            {"reference": reference},
            token=auth_token,
        )
        data = payload.get("data", {})
        return WithdrawalStatus(
            reference=reference,
            status=data.get("status", "unknown"),
            transaction_id=(str(data["transaction_id"]) if data.get("transaction_id") else None),
            amount=_dec(data.get("amount")),
            raw=payload,
        )

    # ------------------------------------------------------------------
    def withdraw(
        self,
        *,
        amount: Decimal,
        method: str,
        recipient: str,
        reference: str,
    ) -> WithdrawalResult:
        """Enchaine les 3 etapes. A n'appeler QUE depuis le worker
        serialise de decaissement (cf. transactions.tasks), jamais
        depuis une vue HTTP.
        """
        auth_token = self.authenticate()
        withdrawal_token, formatted = self.withdrawal_token(
            auth_token=auth_token,
            amount=amount,
            method=method,
            recipient=recipient,
            reference=reference,
        )
        return self.execute_withdrawal(
            withdrawal_token=withdrawal_token,
            amount=formatted,
            method=method,
            recipient=recipient,
            reference=reference,
        )


def get_client() -> PlopPlopClient:
    from django.conf import settings

    return PlopPlopClient(
        base_url=settings.PLOPPLOP["BASE_URL"],
        client_id=settings.PLOPPLOP["CLIENT_ID"],
        client_secret=settings.PLOPPLOP["CLIENT_SECRET"],
        timeout=settings.PLOPPLOP["TIMEOUT"],
    )
