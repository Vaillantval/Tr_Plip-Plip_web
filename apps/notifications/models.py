from __future__ import annotations

from django.conf import settings
from django.db import models


class Channel(models.TextChoices):
    SMS = "sms", "SMS"


class Template(models.TextChoices):
    TRANSFER_COMPLETED = "transfer_completed", "Transfert livre"
    TRANSFER_REFUNDED = "transfer_refunded", "Transfert rembourse"
    #: Avis seulement : le contenu de la reponse n'est JAMAIS dans le SMS.
    #: Un SMS s'affiche sur un ecran verrouille, et le telephone partage
    #: est la norme. Le site reste le canal de l'information.
    CLAIM_ANSWERED = "claim_answered", "Reponse a une reclamation"


class Status(models.TextChoices):
    PENDING = "pending", "A envoyer"
    SENT = "sent", "Envoye"
    FAILED = "failed", "Echec definitif"
    SKIPPED = "skipped", "Non envoye"


class Notification(models.Model):
    """Un envoi, et un seul, par (transaction, modele, canal).

    La ligne est ecrite AVANT l'envoi, dans la transaction de base qui
    porte le changement d'etat. Elle est a la fois le verrou d'idempotence
    et le journal demande en exploitation.

    Pourquoi avant : creee apres l'envoi, un processus tue entre la
    reponse de Twilio et l'ecriture laisserait un SMS parti sans trace, et
    le rejeu en enverrait un second. Creee avant, une annulation l'emporte
    avec le reglement, et un plantage laisse une ligne « a envoyer » que
    le rejeu reprend proprement.

    Le trou restant -- tue entre l'acceptation par Twilio et l'ecriture de
    « envoye » -- est ferme par la cle d'idempotence envoyee a Twilio, qui
    dedoublonne de son cote.
    """

    transaction = models.ForeignKey(
        "transactions.Transaction", on_delete=models.PROTECT, related_name="notifications"
    )
    customer = models.ForeignKey(
        "accounts.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="notifications"
    )
    channel = models.CharField(max_length=16, choices=Channel.choices, default=Channel.SMS)
    template = models.CharField(max_length=32, choices=Template.choices)
    #: Forme canonique 509XXXXXXXX ; l'E.164 est construit a l'envoi.
    recipient = models.CharField(max_length=16, blank=True)
    language = models.CharField(max_length=5, blank=True)
    #: Texte exact expedie, pour que le support lise ce que le client a lu.
    body = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    provider_message_id = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    error_message = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=["transaction", "template", "channel"],
                name="one_notification_per_transaction_and_template",
            )
        ]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self) -> str:
        return f"{self.get_template_display()} vers {self.recipient} ({self.status})"

    @property
    def idempotency_key(self) -> str:
        """Cle stable transmise a Twilio : un rejeu ne double pas le SMS."""
        return f"plipplip-{self.transaction_id}-{self.template}-{self.channel}"
