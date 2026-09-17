"""Reclamations : ce que le client dit quand son transfert a mal tourne.

REGLE QUI DOMINE TOUT : une reclamation ne modifie JAMAIS l'etat d'une
transaction. Elle n'ecrit pas au grand livre, ne rembourse rien, ne
relance rien. Pour agir sur l'argent, l'operateur passe par le detail de
la transaction, comme avant. Un test d'architecture le verifie.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class Reason(models.TextChoices):
    """Ce que le client dit. C'est la donnee que lira l'operateur : deux
    motifs indistinguables lui feraient traiter la mauvaise reclamation.
    """

    NOT_RECEIVED = "not_received", _("Le bénéficiaire n'a rien reçu")
    WRONG_AMOUNT = "wrong_amount", _("Le bénéficiaire a reçu un autre montant")
    WRONG_RECIPIENT = "wrong_recipient", _("L'argent est parti sur un autre numéro")
    CHARGED_TWICE = "charged_twice", _("J'ai payé deux fois le même envoi")
    OTHER = "other", _("Autre chose")


class Status(models.TextChoices):
    OPEN = "open", _("Ouverte")
    ANSWERED = "answered", _("Répondue")
    CLOSED = "closed", _("Close")


class Author(models.TextChoices):
    """Non traduit : ces libelles ne sortent que dans la console, qui est
    en francais. Cote client, le gabarit dit « Vous » ou « Plip-Plip »."""

    CUSTOMER = "customer", "Client"
    OPERATOR = "operator", "Operateur"


#: Une reclamation non close occupe la transaction : le client n'en ouvre
#: pas une seconde a cote, il continue celle-la.
ACTIVE_STATES = (Status.OPEN, Status.ANSWERED)


class Claim(models.Model):
    transaction = models.ForeignKey(
        "transactions.Transaction", on_delete=models.PROTECT, related_name="claims"
    )
    customer = models.ForeignKey("accounts.Customer", on_delete=models.PROTECT, related_name="claims")
    reason = models.CharField(max_length=32, choices=Reason.choices)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="claims_closed"
    )
    #: Derniere fois que le client a ouvert la page. Sert a la pastille,
    #: a rien d'autre.
    seen_by_customer_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=["transaction"],
                condition=models.Q(status__in=[Status.OPEN, Status.ANSWERED]),
                name="one_active_claim_per_transaction",
            )
        ]

    def __str__(self) -> str:
        return f"Reclamation {self.pk} sur {self.transaction.reference}"

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATES

    @property
    def has_unread_answer(self) -> bool:
        """Une reponse d'operateur plus recente que la derniere visite."""
        if self.answered_at is None:
            return False
        return self.seen_by_customer_at is None or self.answered_at > self.seen_by_customer_at


class ClaimMessage(models.Model):
    """Journal des echanges, append-only comme TransactionEvent.

    Ce qui a ete dit au client ne se reecrit pas : c'est la trace de ce
    qu'on lui a promis.
    """

    claim = models.ForeignKey(Claim, on_delete=models.PROTECT, related_name="messages")
    author_kind = models.CharField(max_length=16, choices=Author.choices)
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="claim_messages"
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")

    def __str__(self) -> str:
        return f"{self.get_author_kind_display()} sur reclamation {self.claim_id}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise RuntimeError("ClaimMessage est append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("ClaimMessage est append-only")
