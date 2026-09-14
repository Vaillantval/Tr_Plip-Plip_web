from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    """Roles d'exploitation.

    Separes des permissions Django pour rester lisibles dans la console.
    OPERATOR et au-dessus peuvent declencher des actions sur l'argent ;
    SUPPORT et AUDITOR sont en lecture. La base du Maker-Checker-Approver
    de la phase Payroll est deja posee ici.
    """

    SUPERADMIN = "superadmin", "Superadmin"
    OPERATOR = "operator", "Operateur"
    APPROVER = "approver", "Approbateur"
    SUPPORT = "support", "Support"
    AUDITOR = "auditor", "Auditeur"


#: Roles autorises a declencher une action financiere.
ACTING_ROLES = (Role.SUPERADMIN, Role.OPERATOR)
#: Roles autorises a approuver un lot (phase Payroll).
APPROVING_ROLES = (Role.SUPERADMIN, Role.APPROVER)


class User(AbstractUser):
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.SUPPORT)
    phone = models.CharField(max_length=16, blank=True)

    @property
    def can_act(self) -> bool:
        return self.role in ACTING_ROLES

    @property
    def can_approve(self) -> bool:
        return self.role in APPROVING_ROLES

    @property
    def is_read_only(self) -> bool:
        return self.role in (Role.SUPPORT, Role.AUDITOR)


class AuditLog(models.Model):
    """Journal d'audit de la console. Append-only.

    Toute action declenchee depuis le dashboard y laisse une trace, y
    compris les tentatives refusees.
    """

    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_logs")
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=128, blank=True)
    allowed = models.BooleanField(default=True)
    detail = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.user} {self.action} {self.target}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise RuntimeError("AuditLog est append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("AuditLog est append-only")
