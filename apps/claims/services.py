"""Services des reclamations.

Aucun couplage a `request`, a la session, ni a un objet de vue. Ces
fonctions recoivent le client et des valeurs simples, et levent des
exceptions metier typees qui PORTENT le message destine au client. Elles
ne formatent rien et ne renvoient aucune reponse HTTP : le site et, plus
tard, l'API le rendront chacun a leur facon.

Critere tenu en ecrivant chaque fonction : une vue DRF pourrait-elle
l'appeler telle quelle ? Si l'ajout des routes demandait un refactor, le
decoupage serait mauvais.

Et la regle qui domine : rien ici ne touche a l'etat d'une transaction,
au grand livre, ni au float.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import ACTIVE_STATES, Author, Claim, ClaimMessage, Reason, Status

logger = logging.getLogger(__name__)

#: Bornes de saisie. Assez large pour raconter, assez court pour rester
#: lisible par un operateur qui en traite trente.
BODY_MAX_LENGTH = 2000
BODY_MIN_LENGTH = 5


class ClaimError(Exception):
    """Refus metier. `message` est destine au client, tel quel."""

    message = _("Votre demande n'a pas pu être enregistrée.")

    def __init__(self, message=None):
        self.message = message or self.message
        super().__init__(str(self.message))


class ClaimNotAllowed(ClaimError):
    message = _("Ce transfert ne peut pas faire l'objet d'une réclamation.")


class ClaimAlreadyOpen(ClaimError):
    message = _("Une réclamation est déjà ouverte pour ce transfert. Vous pouvez la compléter.")


class ClaimClosed(ClaimError):
    message = _("Cette réclamation est close. Ouvrez-en une nouvelle si le problème persiste.")


class InvalidBody(ClaimError):
    message = _("Expliquez en quelques mots ce qui s'est passé.")


class InvalidReason(ClaimError):
    message = _("Choisissez un motif dans la liste.")


def clean_body(body: str) -> str:
    text = (body or "").strip()
    if len(text) < BODY_MIN_LENGTH:
        raise InvalidBody()
    if len(text) > BODY_MAX_LENGTH:
        raise InvalidBody(
            _("Votre message est trop long : %(max)s caractères au maximum.") % {"max": BODY_MAX_LENGTH}
        )
    return text


def active_claim(transaction) -> Claim | None:
    return transaction.claims.filter(status__in=ACTIVE_STATES).first()


@db_transaction.atomic
def open_claim(*, customer, transaction, reason: str, body: str) -> Claim:
    """Ouvre une reclamation sur un transfert du client.

    Le cloisonnement par client est verifie ICI aussi, pas seulement dans
    la vue : un service qu'une vue DRF appellera un jour ne peut pas
    dependre de la garde de l'appelant.
    """
    if transaction.customer_id != customer.pk:
        raise ClaimNotAllowed()
    if reason not in Reason.values:
        raise InvalidReason()
    text = clean_body(body)

    try:
        claim = Claim.objects.create(customer=customer, transaction=transaction, reason=reason)
    except IntegrityError as exc:
        # La contrainte d'unicite partielle a tranche : deux envois
        # simultanes du formulaire ne font qu'une reclamation.
        raise ClaimAlreadyOpen() from exc

    _write(claim, kind=Author.CUSTOMER, body=text)
    return claim


@db_transaction.atomic
def add_customer_message(*, customer, claim: Claim, body: str) -> ClaimMessage:
    if claim.customer_id != customer.pk:
        raise ClaimNotAllowed()
    if not claim.is_active:
        raise ClaimClosed()
    message = _write(claim, kind=Author.CUSTOMER, body=clean_body(body))
    # Le client reprend la parole : la reclamation redevient a traiter.
    Claim.objects.filter(pk=claim.pk).update(status=Status.OPEN)
    claim.status = Status.OPEN
    return message


@db_transaction.atomic
def answer_claim(*, claim: Claim, actor, body: str) -> ClaimMessage:
    """Reponse d'un operateur. Declenche l'avis SMS a la PREMIERE reponse."""
    if not claim.is_active:
        raise ClaimClosed()
    text = clean_body(body)
    first_answer = claim.answered_at is None
    message = _write(claim, kind=Author.OPERATOR, body=text, author=actor)

    # answered_at porte la DERNIERE reponse : c'est elle que compare la
    # pastille. L'avis SMS, lui, ne part qu'a la premiere.
    claim.status = Status.ANSWERED
    claim.answered_at = timezone.now()
    claim.save(update_fields=["status", "answered_at"])

    if first_answer:
        _notify_answer(claim)
    return message


@db_transaction.atomic
def close_claim(*, claim: Claim, actor, note: str = "") -> Claim:
    """Cloture. N'envoie RIEN : une reclamation close sans reponse ne
    donne rien a lire au client."""
    if claim.status == Status.CLOSED:
        raise ClaimClosed()
    if note.strip():
        _write(claim, kind=Author.OPERATOR, body=clean_body(note), author=actor)
    claim.status = Status.CLOSED
    claim.closed_at = timezone.now()
    claim.closed_by = actor
    claim.save(update_fields=["status", "closed_at", "closed_by"])
    return claim


def unread_transaction_ids(customer) -> set[int]:
    """Transferts dont la reponse n'a pas encore ete lue, pour la pastille.

    Le site est le canal principal : le SMS ne fait qu'y ramener le
    client. Un SMS non recu ne doit jamais etre le seul chemin vers
    l'information.
    """
    from django.db.models import F, Q

    return set(
        Claim.objects.filter(customer=customer, answered_at__isnull=False)
        .filter(Q(seen_by_customer_at__isnull=True) | Q(answered_at__gt=F("seen_by_customer_at")))
        .values_list("transaction_id", flat=True)
    )


def mark_seen(claim: Claim) -> None:
    """Le client a ouvert la page : la pastille s'eteint."""
    Claim.objects.filter(pk=claim.pk).update(seen_by_customer_at=timezone.now())


def _write(claim: Claim, *, kind: str, body: str, author=None) -> ClaimMessage:
    return ClaimMessage.objects.create(claim=claim, author_kind=kind, body=body, author=author)


def _notify_answer(claim: Claim) -> None:
    """Avis SMS, jamais bloquant.

    Un echec d'envoi ne doit pas empecher un operateur de repondre : le
    site reste le canal principal, le SMS ne fait qu'y ramener le client.
    """
    from apps.notifications import services as notifications

    try:
        notifications.notify_claim_answered(claim.transaction)
    except Exception:
        logger.exception("Avis de reponse non programme pour la reclamation %s", claim.pk)
