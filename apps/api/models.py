from __future__ import annotations

from django.db import models


class IdempotencyKey(models.Model):
    """Cle Idempotency-Key d'une creation de transfert.

    Une cle rejouee rend la transaction deja creee au lieu d'en creer une
    seconde : c'est ce qui protege contre le double tap et les reseaux
    mobiles instables. L'empreinte du corps interdit de reutiliser une
    cle pour une demande differente.
    """

    customer = models.ForeignKey("accounts.Customer", on_delete=models.CASCADE, related_name="idempotency_keys")
    key = models.CharField(max_length=64)
    request_fingerprint = models.CharField(max_length=64)
    transaction = models.OneToOneField(
        "transactions.Transaction", on_delete=models.PROTECT, related_name="idempotency_key"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["customer", "key"], name="unique_idempotency_key_per_customer")]

    def __str__(self) -> str:
        return f"{self.customer} {self.key}"
