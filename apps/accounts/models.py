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


class NotificationChannel(models.TextChoices):
    SMS = "sms", "SMS"
    WHATSAPP = "whatsapp", "WhatsApp"


class AuthMethod(models.TextChoices):
    """Methode de la connexion : retenue, jamais interpretee.

    Un code est « quelque chose qu'on possede », un mot de passe
    « quelque chose qu'on sait ». Si la BRH impose un second facteur
    au-dela d'un montant, il suffira d'exiger une session ouverte par code.
    """

    CODE = "code", "Code SMS"
    PASSWORD = "password", "Mot de passe"


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
    #: Canal des avis de transfert, choisi par le client sur la page de
    #: confirmation. WhatsApp retombe sur le SMS quand il ne peut pas livrer.
    notification_channel = models.CharField(
        max_length=16, choices=NotificationChannel.choices, default=NotificationChannel.SMS
    )
    #: Moment ou le client a choisi WhatsApp : la preuve de son accord,
    #: exigee par Meta. Remis a vide s'il revient au SMS.
    whatsapp_opt_in_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    # --- Mot de passe : facultatif, propose apres la premiere connexion. ---
    # Hache par django.contrib.auth.hashers (make_password), SANS heriter
    # d'AbstractBaseUser : un client reste etranger a django.contrib.auth.
    # Vide = pas de mot de passe, connexion par code uniquement.
    password = models.CharField(max_length=128, blank=True, default="")
    password_set_at = models.DateTimeField(null=True, blank=True)
    #: Augmente a chaque reinitialisation ou changement de mot de passe.
    #: Copie dans la session a la connexion : une session dont la valeur ne
    #: correspond plus est refusee. Seul moyen d'invalider toutes les
    #: sessions web d'un client sans les retrouver une a une.
    auth_epoch = models.PositiveIntegerField(default=0)
    #: Verrouillage PAR COMPTE, en base et non en cache : une eviction
    #: Redis ne doit pas rendre un compte de nouveau attaquable.
    password_failures = models.PositiveIntegerField(default=0)
    password_locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.phone

    @property
    def has_password(self) -> bool:
        return bool(self.password)

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
    #: Les jetons anterieurs viennent tous d'un code : d'ou la valeur par defaut.
    auth_method = models.CharField(max_length=16, choices=AuthMethod.choices, default=AuthMethod.CODE)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.prefix}… ({self.customer})"


class AuthEvent(models.TextChoices):
    PASSWORD_FAILED = "password_failed", "Mot de passe refusé"
    ACCOUNT_LOCKED = "account_locked", "Compte verrouillé"
    IP_LOCKED = "ip_locked", "Adresse IP bloquée"
    PASSWORD_SET = "password_set", "Mot de passe créé"
    PASSWORD_CHANGED = "password_changed", "Mot de passe changé"
    PASSWORD_RESET = "password_reset", "Mot de passe réinitialisé"


class CustomerAuthEvent(models.Model):
    """Journal des connexions clients. Append-only.

    Separe d'AuditLog, qui repond a « qui, dans l'equipe, a fait quoi dans
    la console » : son `user` est un operateur. Les clients y seraient des
    intrus, et l'ecran d'audit devrait les trier.

    `phone` est le numero SAISI, normalise, meme inconnu : c'est le seul
    moyen de voir une attaque sur des numeros qui n'existent pas. Jamais
    de mot de passe ni de code, ici ou dans `detail`.
    """

    phone = models.CharField(max_length=16, blank=True, db_index=True)
    customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="auth_events"
    )
    event = models.CharField(max_length=32, choices=AuthEvent.choices, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("-created_at", "-id")

    def __str__(self) -> str:
        return f"{self.event} {self.phone or self.ip_address}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise RuntimeError("CustomerAuthEvent est append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("CustomerAuthEvent est append-only")


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
