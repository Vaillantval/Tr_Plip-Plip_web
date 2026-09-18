"""Plafonds cumules sur fenetres glissantes.

Deux sujets, deux natures :

  - par CLIENT (expediteur) : une limite que nous lui promettons ;
  - par NUMERO BENEFICIAIRE : une borne de surveillance anti-blanchiment.

Le second n'est pas une extension du premier. Un beneficiaire n'est pas un
client : il n'a jamais rien cree. Seule _conditions() est partagee.


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


# ----------------------------------------------------------------------
# Plafond du BENEFICIAIRE
#
# Un beneficiaire n'est PAS un client : il n'a jamais rien cree, il existe
# uniquement comme recipient_phone sur une transaction. Rien de ce qui suit
# ne generalise les fonctions ci-dessus -- seule _conditions() est partagee,
# parce qu'elle ne mentionne aucun sujet.
#
# Et ce plafond n'a pas la meme nature que celui du client. Le plafond
# expediteur est une limite que nous PROMETTONS ; celui-ci est une borne de
# SURVEILLANCE. Beaucoup d'expediteurs vers un meme numero est le schema que
# la lutte anti-blanchiment cherche a detecter, et il ne se lit nulle part
# aujourd'hui. Sa valeur est dans la trace qu'il laisse, plus que dans le
# refus lui-meme : quelqu'un de determine changera de numero.
# ----------------------------------------------------------------------

#: Refus pour plafond beneficiaire tolerees avant que la reponse cesse de
#: dependre du montant. Bas volontairement : un expediteur legitime qui se
#: heurte a un numero sature s'arrete ou change de numero. Trois refus sur
#: ce motif en une heure sont deja anormaux.
PROBE_MAX_REFUSALS = 3
PROBE_WINDOW_SECONDS = 3600


class RecipientLimitExceeded(ValueError):
    """Plafond du beneficiaire atteint.

    Porte ce qu'il faut a l'AUDIT, jamais ce qu'on dira au client :
    l'expediteur n'est pas en cause et n'a aucun droit de connaitre
    l'activite d'un tiers. Le texte client est une CONSTANTE, il ne se
    calcule pas a partir de cette exception -- ne jamais rendre str(exc)
    sur une surface publique.
    """

    def __init__(
        self,
        message: str,
        *,
        phone: str,
        amount: Decimal,
        window: LimitWindow | None = None,
        senders: int = 0,
        throttled: bool = False,
    ):
        super().__init__(message)
        self.phone = phone
        self.amount = amount
        #: None quand le refus vient de l'etranglement : on n'a pas consulte
        #: le plafond, donc on n'a rien a en dire.
        self.window = window
        self.senders = senders
        self.throttled = throttled


def _counted_for_recipient(phone: str, *, since: datetime, now: datetime):
    """Transferts recus par ce numero sur la fenetre.

    Aucun filtre sur le client : un transfert cree depuis la console compte
    dans la consommation du beneficiaire meme s'il n'est jamais refuse.
    Le controle ne s'applique pas a l'operateur, le compteur si -- sinon un
    numero qui recoit beaucoup par la console devient invisible au capteur,
    exactement la ou l'on voudrait voir.
    """
    collected, reserved = _conditions(since=since, now=now)
    return Transaction.objects.filter(recipient_phone=phone).filter(collected | reserved)


def recipient_consumption(phone: str, *, now=None, policy=None) -> list[LimitWindow]:
    """Ce que ce numero a recu sur les deux fenetres. Lecture seule."""
    now = now or timezone.now()
    policy = policy or limit_policy()
    caps = {DAY: policy.recipient_daily_cap, MONTH: policy.recipient_monthly_cap}
    windows = []
    for name, seconds in window_seconds().items():
        since = now - timedelta(seconds=seconds)
        collected, reserved = _conditions(since=since, now=now)
        sums = _counted_for_recipient(phone, since=since, now=now).aggregate(
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


def distinct_senders(phone: str, window: LimitWindow, *, now=None) -> int:
    """Expediteurs distincts vers ce numero sur la fenetre.

    LE signal de ce capteur. Quinze expediteurs vers un meme numero ne se
    lit nulle part aujourd'hui -- ni ecran, ni journal, ni alerte. Les
    transferts de console n'ont pas de client et ne comptent pas ici.
    """
    now = now or timezone.now()
    since = now - timedelta(seconds=window.seconds)
    return (
        _counted_for_recipient(phone, since=since, now=now)
        .filter(customer__isnull=False)
        .values("customer_id")
        .distinct()
        .count()
    )


def _probe_key(customer) -> str:
    return f"limits:recipient-probe:{customer.pk}"


def _probe_blocked(customer) -> bool:
    from django.core.cache import cache

    return (cache.get(_probe_key(customer)) or 0) >= PROBE_MAX_REFUSALS


def _probe_record(customer) -> None:
    """Compte un refus. Fenetre fixe, comme apps/web/ratelimit."""
    from django.core.cache import cache

    key = _probe_key(customer)
    if cache.add(key, 1, PROBE_WINDOW_SECONDS):
        return
    try:
        cache.incr(key)
    except ValueError:  # cle expiree entre add et incr
        cache.set(key, 1, PROBE_WINDOW_SECONDS)


def check_recipient_allowed(phone: str, *, net_amount: Decimal, customer, now=None) -> None:
    """Leve RecipientLimitExceeded si ce numero ne peut pas recevoir ce montant.

    AUCUN VERROU ici, contrairement au plafond du client, et ce n'est pas un
    oubli. Le plafond expediteur est une limite que nous PROMETTONS au
    client : un depassement serait une promesse rompue, d'ou le
    select_for_update. Celui-ci est une borne de SURVEILLANCE : sa valeur
    est dans la trace qu'il laisse, pas dans son etancheite a la
    milliseconde. Et il n'y a aucune ligne a verrouiller -- un beneficiaire
    n'est pas un client. Deux creations simultanees vers le meme numero
    peuvent donc depasser d'UN transfert au plus ; la suivante est refusee.
    Quelqu'un qui blanchit ne synchronise pas deux transferts a la
    milliseconde pres, il change de numero, ce qu'aucun verrou n'arrete.

    N'ECRIT PAS la trace du refus : comme pour le plafond client, nous
    sommes dans la transaction de base qui va etre annulee. C'est
    audit_recipient_refusal() qui l'ecrit, apres l'annulation.
    """
    now = now or timezone.now()

    # ETRANGLEMENT DU SONDAGE.
    #
    # Le message rendu au client est une constante, sans chiffre ni date.
    # Cela ne suffit pas : c'est la frontiere acceptee/refusee qui parle. Un
    # expediteur qui essaie des montants decroissants jusqu'a ce que ca
    # passe -- refuse a 30 000, accepte a 12 000 -- connait la marge
    # restante du beneficiaire a quelques milliers pres, et la reconstitue
    # jour apres jour.
    #
    # Passe ce seuil, on refuse SANS consulter le plafond : la meme reponse,
    # qu'un montant serait passe ou non. La recherche dichotomique ne
    # converge plus.
    #
    # On ne supprime pas l'oracle, on le rend lent et bruyant. Tant que la
    # reponse depend du montant demande, un sondage assez patient finit par
    # deduire la marge restante. Le seul remede complet serait de refuser
    # identiquement TOUS les transferts vers un numero des le premier refus,
    # ce qui punirait des expediteurs innocents pour l'activite d'un tiers.
    # C'est un arbitrage, pas une etancheite : le prochain lecteur doit le
    # savoir avant de croire le canal ferme.
    #
    # POURQUOI CE COMPTEUR VIT ICI et non dans apps/web/ratelimit.py, ou
    # vivent les autres limites de debit : ce module-ci est traverse par le
    # site ET par l'API. Dans la couche web, seul le site serait protege --
    # et l'API est justement le canal qu'un sondeur automatise choisirait,
    # parce qu'elle repond en JSON, sans page a analyser. Une protection
    # qui ne couvre pas le chemin le plus commode n'en est pas une.
    # (apps.transactions ne doit de toute facon pas importer apps.web.)
    if _probe_blocked(customer):
        _probe_record(customer)
        raise RecipientLimitExceeded(
            f"Refus sans consultation du plafond : {PROBE_MAX_REFUSALS} refus deja comptes "
            f"pour ce client en {PROBE_WINDOW_SECONDS} s",
            phone=phone,
            amount=net_amount,
            throttled=True,
        )

    for window in recipient_consumption(phone, now=now):
        if window.allows(net_amount):
            continue
        _probe_record(customer)
        raise RecipientLimitExceeded(
            f"Plafond beneficiaire {window.name} atteint pour {phone} : "
            f"{window.used} + {net_amount} > {window.cap} HTG",
            phone=phone,
            amount=net_amount,
            window=window,
            senders=distinct_senders(phone, window, now=now),
        )


def audit_recipient_refusal(exc: RecipientLimitExceeded) -> None:
    """Trace d'un refus pour plafond beneficiaire. HORS transaction de base.

    Cette ligne vaut plus que le refus. Elle porte le numero vise, la
    fenetre atteinte, le montant demande -- une suite de montants
    decroissants vers un meme numero est un signal de sondage -- et surtout
    le nombre d'expediteurs distincts, qui est ce que ce capteur existe pour
    montrer.
    """
    from apps.accounts.models import AuditLog

    detail = {
        "requested": str(exc.amount),
        "throttled": exc.throttled,
    }
    if exc.window is not None:
        detail |= {
            "window": exc.window.name,
            "cap": str(exc.window.cap),
            "used": str(exc.window.used),
            "senders": exc.senders,
        }
    AuditLog.objects.create(
        user=None,
        action="limit.recipient_refused",
        target=exc.phone,
        allowed=False,
        detail=detail,
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
