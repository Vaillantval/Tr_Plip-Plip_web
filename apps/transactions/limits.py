"""Plafonds cumules par client, sur fenetres glissantes.

Le plafond par transaction (PRICING["MAX_NET_AMOUNT"]) protege d'une faute
de frappe. Ceux-ci protegent d'un cumul : c'est une obligation applicable a
un service de transfert de fonds, pas une commodite.

Deux regles structurantes :

  - VERIFIE A LA CREATION, jamais apres. Refuser une transaction dont nous
    avons deja pris l'argent serait ingerable : il faudrait rembourser.
  - CALCULE, jamais stocke. Aucun compteur, aucune ecriture au grand livre :
    un compteur exigerait une contre-ecriture a chaque remboursement et
    finirait par deriver. Un remboursement libere le plafond par simple
    absence de la somme.

Ce qui consomme :
  - les transferts REELLEMENT ENCAISSES et non rembourses, dates par
    payment_confirmed_at -- l'instant ou nous avons pris l'argent ;
  - les transferts en attente de paiement dont le delai court encore
    (« reservation »). Sans eux, un client creerait dix transferts au
    plafond sans en payer aucun, puis les paierait tous : le controle a la
    creation ne verrait rien venir. Une reservation qui expire disparait
    d'elle-meme, comme un remboursement.

Un devis n'ecrit rien, donc ne consomme rien.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

from .models import Transaction, TransferLimitPolicy
from .states import LIABILITY_STATES, State

ZERO = Decimal("0")
CENT = Decimal("0.01")

DAY = "day"
MONTH = "month"

#: L'argent est pris et ne sera pas rendu.
CONSUMING_STATES = tuple(LIABILITY_STATES) + (State.COMPLETED,)
#: Paiement demande, pas encore obtenu : reserve tant qu'il peut aboutir.
RESERVING_STATES = (State.CREATED, State.AWAITING_PAYMENT)


class LimitExceeded(ValueError):
    """Plafond cumule atteint. Ne derive PAS de UnsupportedRoute : les deux
    surfaces client doivent l'attraper explicitement, pas la confondre avec
    une route fermee.
    """

    def __init__(self, message: str, *, window: "LimitWindow", amount: Decimal, frees_at: datetime | None):
        super().__init__(message)
        self.window = window
        self.amount = amount
        self.frees_at = frees_at


@dataclass(frozen=True)
class LimitWindow:
    name: str
    seconds: int
    cap: Decimal
    #: Argent reellement pris et non rendu.
    collected: Decimal
    #: Paiements demandes dont le delai court encore. Distingues du reste :
    #: un operateur au telephone doit pouvoir dire « vous n'avez rien
    #: envoye, un transfert abandonne se libere dans quelques minutes »
    #: plutot que de chercher des transferts qui n'existent pas.
    reserved: Decimal

    @property
    def used(self) -> Decimal:
        # Toujours en centimes : ces montants sortent tels quels dans la
        # reponse API.
        return (self.collected + self.reserved).quantize(CENT)

    @property
    def remaining(self) -> Decimal:
        return max(ZERO, self.cap - self.used).quantize(CENT)

    @property
    def since(self):
        return timedelta(seconds=self.seconds)

    def allows(self, amount: Decimal) -> bool:
        # Strictement superieur : un transfert qui atteint exactement le
        # plafond passe.
        return self.used + amount <= self.cap


def limit_policy() -> TransferLimitPolicy:
    policy, _ = TransferLimitPolicy.objects.get_or_create(pk=1)
    return policy


def window_seconds() -> dict[str, int]:
    return {DAY: settings.TRANSFER_LIMITS[DAY], MONTH: settings.TRANSFER_LIMITS[MONTH]}


def _conditions(*, since: datetime, now: datetime) -> tuple[Q, Q]:
    collected = Q(state__in=CONSUMING_STATES, payment_confirmed_at__gte=since)
    reserved = Q(state__in=RESERVING_STATES, payment_expires_at__gt=now)
    return collected, reserved


def _counted(customer, *, since: datetime, now: datetime):
    """Transferts qui consomment le plafond sur la fenetre."""
    collected, reserved = _conditions(since=since, now=now)
    return Transaction.objects.filter(customer=customer).filter(collected | reserved)


def customer_consumption(customer, *, now=None, policy=None) -> list[LimitWindow]:
    """Consommation du client sur les deux fenetres. Lecture seule."""
    now = now or timezone.now()
    policy = policy or limit_policy()
    caps = {DAY: policy.daily_cap, MONTH: policy.monthly_cap}
    windows = []
    for name, seconds in window_seconds().items():
        since = now - timedelta(seconds=seconds)
        collected, reserved = _conditions(since=since, now=now)
        sums = _counted(customer, since=since, now=now).aggregate(
            collected=Sum("net_amount", filter=collected),
            reserved=Sum("net_amount", filter=reserved),
        )
        windows.append(
            LimitWindow(
                name=name,
                seconds=seconds,
                cap=caps[name],
                collected=(sums["collected"] or ZERO).quantize(CENT),
                reserved=(sums["reserved"] or ZERO).quantize(CENT),
            )
        )
    return windows


def releases(customer, window: LimitWindow, *, now=None) -> list[tuple[datetime, Decimal]]:
    """(instant de liberation, montant), par ordre croissant.

    Un encaissement se libere `seconds` apres payment_confirmed_at ; une
    reservation se libere a l'expiration de son paiement.
    """
    now = now or timezone.now()
    since = now - timedelta(seconds=window.seconds)
    moments = []
    rows = _counted(customer, since=since, now=now).values_list(
        "state", "net_amount", "payment_confirmed_at", "payment_expires_at"
    )
    for state, amount, confirmed_at, expires_at in rows:
        if state in RESERVING_STATES:
            moments.append((expires_at, amount))
        else:
            moments.append((confirmed_at + timedelta(seconds=window.seconds), amount))
    return sorted(moments)


def frees_at(customer, window: LimitWindow, *, amount: Decimal, now=None) -> datetime | None:
    """Premier instant ou `amount` repasserait sous le plafond.

    None : le montant depasse a lui seul le plafond, aucune attente n'y
    changera rien.
    """
    if amount > window.cap:
        # La somme de toutes les liberations vaut `used` : elle ne peut pas
        # couvrir un montant qui depasse a lui seul le plafond.
        return None
    missing = window.used + amount - window.cap
    freed = ZERO
    for moment, value in releases(customer, window, now=now):
        freed += value
        if freed >= missing:
            return moment
    return None


def check_transfer_allowed(customer, *, net_amount: Decimal, now=None) -> None:
    """Leve LimitExceeded si la creation ferait depasser un plafond.

    A appeler dans la transaction de base qui cree le transfert, apres
    avoir verrouille la ligne du client : deux creations simultanees du
    meme client doivent se suivre, sinon chacune passe sous le plafond et
    les deux le depassent ensemble.

    N'ECRIT PAS la trace du refus : nous sommes dans la transaction de
    base qui va etre annulee, la ligne d'audit disparaitrait avec elle.
    C'est audit_limit_refusal(), appelee APRES l'annulation, qui l'ecrit.
    """
    now = now or timezone.now()
    for window in customer_consumption(customer, now=now):
        if window.allows(net_amount):
            continue
        moment = frees_at(customer, window, amount=net_amount, now=now)
        raise LimitExceeded(
            f"Plafond {window.name} atteint : {window.used} + {net_amount} > {window.cap} HTG",
            window=window,
            amount=net_amount,
            frees_at=moment,
        )


def audit_limit_refusal(exc: LimitExceeded, *, customer) -> None:
    """Trace d'un refus pour plafond. A appeler HORS transaction de base.

    Sans cette trace, on ne saura jamais si les plafonds bloquent dix
    personnes par jour ou zero -- et donc s'il faut les relever.
    """
    from apps.accounts.models import AuditLog

    AuditLog.objects.create(
        user=None,
        action="limit.refused",
        target=customer.phone,
        allowed=False,
        detail={
            "window": exc.window.name,
            "cap": str(exc.window.cap),
            "used": str(exc.window.used),
            "requested": str(exc.amount),
            "frees_at": exc.frees_at.isoformat() if exc.frees_at else None,
        },
    )
