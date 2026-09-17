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

    @property
    def is_superadmin(self) -> bool:
        return self.role == Role.SUPERADMIN


class Customer(models.Model):
    """Client de l'API publique, identifie par son telephone.

    Volontairement distinct de User : un client n'est pas un utilisateur
    Django. Il ne peut donc ni ouvrir de session sur la console ni sur
    l'admin, quelle que soit la faille d'une vue -- par construction.
    """

    phone = models.CharField(max_length=16, unique=True)
    is_active = models.BooleanField(default=True)
    #: Langue de la derniere connexion reussie. Vide = jamais observee.
    #: Une tache Celery n'a ni requete ni cookie : sans ce champ, un SMS
    #: partirait toujours en francais.
    language = models.CharField(max_length=5, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.phone

    # Interface attendue par DRF pour request.user.
    is_authenticated = True
    is_anonymous = False


class CustomerToken(models.Model):
    """Jeton d'acces a l'API. Seule son empreinte SHA-256 est stockee."""

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="tokens")
    key_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.prefix}… ({self.customer})"


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
