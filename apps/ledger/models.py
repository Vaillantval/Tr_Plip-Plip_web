"""Grand livre en partie double, append-only.

Sans ce module, la reconciliation decrite dans la note conceptuelle est
inecrivable : un champ `status` sur Transaction dit ou en est une
operation, il ne dit pas ou est l'argent.

Regle unique et non negociable : la somme des lignes d'une ecriture est
toujours nulle. Une ecriture publiee n'est jamais modifiee ni supprimee
-- une erreur se corrige par une contre-ecriture.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models


class AccountType(models.TextChoices):
    ASSET = "asset", "Actif"
    LIABILITY = "liability", "Passif"
    REVENUE = "revenue", "Produit"
    EXPENSE = "expense", "Charge"


class LedgerAccount(models.Model):
    """Comptes du plan comptable interne.

    Comptes attendus au demarrage :
        float.plopplop     ASSET      solde prepaye marchand
        clients.payable    LIABILITY  fonds encaisses non encore verses
        revenue.commission REVENUE    commission Plip-Plip
        expense.fees       EXPENSE    frais operateurs
        cash.settlement    ASSET      contrepartie des encaissements
    """

    code = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=128)
    type = models.CharField(max_length=16, choices=AccountType.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("code",)

    def __str__(self) -> str:
        return f"{self.code} — {self.label}"

    def balance(self) -> Decimal:
        agg = self.lines.aggregate(total=models.Sum("amount"))
        return agg["total"] or Decimal("0")


class JournalEntry(models.Model):
    """Une ecriture equilibree, rattachee a un evenement metier."""

    reference = models.CharField(max_length=64, db_index=True)
    description = models.CharField(max_length=255)
    transaction = models.ForeignKey(
        "transactions.Transaction",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="journal_entries",
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="journal_entries"
    )
    reverses = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="reversed_by"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name_plural = "journal entries"

    def __str__(self) -> str:
        return f"{self.reference}: {self.description}"

    def total(self) -> Decimal:
        agg = self.lines.aggregate(total=models.Sum("amount"))
        return agg["total"] or Decimal("0")

    def is_balanced(self) -> bool:
        return self.total() == Decimal("0")

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise RuntimeError("JournalEntry est append-only : passer par une contre-ecriture")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("JournalEntry est append-only")


class LedgerLine(models.Model):
    """Ligne d'ecriture. Montant signe : positif au debit, negatif au credit."""

    entry = models.ForeignKey(JournalEntry, on_delete=models.PROTECT, related_name="lines")
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="lines")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    memo = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("entry_id", "id")

    def __str__(self) -> str:
        return f"{self.account.code} {self.amount:+}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise RuntimeError("LedgerLine est append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("LedgerLine est append-only")
