"""Controle d'admission : refuser plutot que retenir l'argent.

Le cooldown plopplop fixe le debit a environ 28 decaissements par heure,
pour toute la plateforme. Si les creations depassent durablement ce
rythme, la file s'allonge sans fin -- et chaque ligne de cette file est
l'argent d'un client que nous detenons sans pouvoir le livrer.

Un client a qui l'on dit « pas maintenant » repart avec son argent. Un
client dont on encaisse le paiement et qu'on fait attendre trois jours
appelle le support, puis sa banque. Refuser a l'entree est la seule
protection que nous controlons entierement : le reste depend de plopplop.

Deux motifs de refus, verifies a la CREATION uniquement, jamais apres :

  - la file est plus longue que ce que nous pouvons ecouler dans le delai
    que nous nous donnons (ADMISSION_MAX_WAIT_SECONDS) ;
  - le worker de decaissement est a l'arret : la file ne s'ecoule plus du
    tout, et encaisser reviendrait a prendre de l'argent sans aucun moyen
    de le rendre au rythme prevu.

Les transactions creees depuis la console (sans client) ne sont jamais
refusees : un operateur qui rattrape une situation ne doit pas etre
bloque par la saturation qu'il est en train de traiter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from . import queue

SATURATED = "queue_full"
STALLED = "payouts_stopped"


@dataclass(frozen=True)
class Saturation:
    """Etat d'admission, tel qu'il peut etre montre a un operateur."""

    reason: str | None
    depth: int
    projected_wait_seconds: int
    max_wait_seconds: int

    @property
    def accepting(self) -> bool:
        return self.reason is None

    @property
    def free_slots(self) -> int:
        """Creations encore acceptees avant fermeture. 0 si deja fermee."""
        if self.reason is not None:
            return 0
        cooldown = settings.PAYOUT_COOLDOWN_SECONDS
        return max(0, self.max_wait_seconds // cooldown - self.depth)


class ServiceSaturated(Exception):
    """Creation refusee : la file ne peut pas absorber ce transfert.

    Ne derive PAS de ValueError ni de UnsupportedRoute : ce n'est ni une
    erreur de saisie ni une route fermee, et les deux surfaces clientes
    doivent l'attraper explicitement.
    """

    def __init__(self, message: str, *, saturation: Saturation, retry_after_seconds: int):
        super().__init__(message)
        self.saturation = saturation
        self.retry_after_seconds = retry_after_seconds


def admission_state(now: datetime | None = None) -> Saturation:
    """Lecture seule : la file accepte-t-elle un transfert de plus ?

    S'appuie sur la vue de file partagee, donc sans relecture complete.
    """
    now = now or timezone.now()
    view = queue.public_queue_view(now=now)
    depth = view.worker.depth
    max_wait = settings.ADMISSION_MAX_WAIT_SECONDS
    # Le transfert qui arriverait maintenant passerait en derniere position.
    projected = (depth + 1) * settings.PAYOUT_COOLDOWN_SECONDS

    reason = None
    if view.worker.stalled:
        reason = STALLED
    elif projected > max_wait:
        reason = SATURATED
    return Saturation(
        reason=reason, depth=depth, projected_wait_seconds=projected, max_wait_seconds=max_wait
    )


def check_admission(now: datetime | None = None) -> None:
    """Leve ServiceSaturated si un nouveau transfert ne peut pas etre tenu."""
    if not settings.ADMISSION_CONTROL_ENABLED:
        return
    state = admission_state(now=now)
    if state.accepting:
        return

    if state.reason == STALLED:
        message = "Decaissements a l'arret : aucune nouvelle creation tant que la file ne s'ecoule pas"
        # Rien ne sert de proposer une heure : personne ne sait quand le
        # worker repartira. Une minute, le temps qu'un operateur agisse.
        retry_after = 60
    else:
        message = (
            f"File saturee : {state.depth} transferts en attente, "
            f"soit {state.projected_wait_seconds // 60} min avant le prochain entrant"
        )
        # Une place se libere a chaque decaissement, donc a chaque cooldown.
        retry_after = settings.PAYOUT_COOLDOWN_SECONDS
    raise ServiceSaturated(message, saturation=state, retry_after_seconds=retry_after)


def audit_refusal(saturation: Saturation, *, customer=None) -> None:
    """Trace d'un refus pour saturation. A appeler HORS transaction de base.

    Sans elle, on ne saura jamais combien de clients ont ete refuses --
    et donc si le seuil est trop bas ou si le debit plopplop est devenu
    le vrai probleme.
    """
    from apps.accounts.models import AuditLog

    AuditLog.objects.create(
        user=None,
        action="admission.refused",
        target=customer.phone if customer is not None else "",
        allowed=False,
        detail={
            "reason": saturation.reason,
            "depth": saturation.depth,
            "projected_wait_seconds": saturation.projected_wait_seconds,
            "max_wait_seconds": saturation.max_wait_seconds,
        },
    )


def reopens_at(saturation: Saturation, now: datetime | None = None) -> datetime | None:
    """Instant estime de reouverture. None si personne ne peut le savoir."""
    if saturation.reason != SATURATED:
        return None
    over = saturation.projected_wait_seconds - saturation.max_wait_seconds
    cooldown = settings.PAYOUT_COOLDOWN_SECONDS
    slots = -(-over // cooldown)  # arrondi vers le haut
    return (now or timezone.now()) + timedelta(seconds=slots * cooldown)
