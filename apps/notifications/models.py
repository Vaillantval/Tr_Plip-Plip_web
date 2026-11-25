from __future__ import annotations

from django.conf import settings
from django.db import models


class Channel(models.TextChoices):
    SMS = "sms", "SMS"
    WHATSAPP = "whatsapp", "WhatsApp"


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


class Provider(models.TextChoices):
    TWILIO = "twilio", "Twilio"
    SENT = "sent", "Sent"


class MessagingSetting(models.Model):
    """Fournisseur actif, choisi par le superadmin. Une seule ligne (pk=1).

    Deux choix independants : les codes de connexion et les avis. On peut
    ainsi passer les codes sur un fournisseur dont le gabarit est approuve
    sans attendre ceux des avis.

    Pas de ligne = Twilio pour les deux. Lire par current(), jamais
    directement : la valeur est mise en cache, chaque envoi la consulte.
    """

    otp_provider = models.CharField(max_length=16, choices=Provider.choices, default=Provider.TWILIO)
    notification_provider = models.CharField(max_length=16, choices=Provider.choices, default=Provider.TWILIO)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    CACHE_KEY = "plipplip:messaging-setting"
    CACHE_SECONDS = 30

    def __str__(self) -> str:
        return f"Codes : {self.otp_provider}, avis : {self.notification_provider}"

    @classmethod
    def current(cls) -> "MessagingSetting":
        from django.core.cache import cache

        values = cache.get(cls.CACHE_KEY)
        if values is None:
            row = cls.objects.filter(pk=1).first() or cls(pk=1)
            values = {"otp_provider": row.otp_provider, "notification_provider": row.notification_provider}
            cache.set(cls.CACHE_KEY, values, cls.CACHE_SECONDS)
        return cls(pk=1, **values)

    @classmethod
    def clear_cache(cls) -> None:
        from django.core.cache import cache

        cache.delete(cls.CACHE_KEY)


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
    #: Fige a la mise en file : une reprise part chez le meme fournisseur,
    #: meme si le superadmin a bascule entre-temps (cle d'idempotence).
    provider = models.CharField(max_length=16, choices=Provider.choices, default=Provider.TWILIO)
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
