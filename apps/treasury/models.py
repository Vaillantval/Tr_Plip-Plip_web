from __future__ import annotations

from django.conf import settings
from django.db import models


class FloatSnapshot(models.Model):
    """Photo du solde prepaye, prise a chaque decaissement reussi.

    plopplop retourne `balance_after` sur chaque retrait : c'est notre
    seule source de verite externe sur le float. On l'enregistre pour
    pouvoir comparer avec le solde calcule par le grand livre -- l'ecart
    entre les deux est l'indicateur de reconciliation le plus utile.
    """

    provider_balance = models.DecimalField(max_digits=14, decimal_places=2)
    ledger_balance = models.DecimalField(max_digits=14, decimal_places=2)
    transaction = models.ForeignKey(
        "transactions.Transaction", null=True, blank=True, on_delete=models.SET_NULL, related_name="float_snapshots"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    @property
    def drift(self):
        return self.provider_balance - self.ledger_balance


class FloatAlert(models.Model):
    class Level(models.TextChoices):
        WARNING = "warning", "Avertissement"
        CRITICAL = "critical", "Critique"

    level = models.CharField(max_length=16, choices=Level.choices)
    balance = models.DecimalField(max_digits=14, decimal_places=2)
    message = models.CharField(max_length=255)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="float_alerts"
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
