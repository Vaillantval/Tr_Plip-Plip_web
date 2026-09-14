"""Garde-fous de la console.

Toute action qui touche a l'argent passe par require_acting_role. Les
refus sont journalises au meme titre que les acceptations : une
tentative refusee est une information d'exploitation.
"""

from __future__ import annotations

from functools import wraps

from django.core.exceptions import PermissionDenied

from apps.accounts.models import AuditLog

#: Longueur maximale d'une valeur de formulaire recopiee dans l'audit.
AUDIT_VALUE_MAX_LENGTH = 255


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def audit(request, action: str, *, target: str = "", allowed: bool = True, **detail) -> None:
    AuditLog.objects.create(
        user=request.user if request.user.is_authenticated else None,
        action=action,
        target=target,
        allowed=allowed,
        detail=detail,
        ip_address=client_ip(request),
    )


def audit_failure(request, action: str, *, target: str = "", error: str, **detail) -> None:
    """Seconde ligne d'audit : issue d'une tentative autorisee qui a echoue."""
    audit(request, action, target=target, allowed=True, outcome="error", error=error, **detail)


def require_acting_role(action: str, *, target_kwarg: str = "reference", audit_fields: tuple[str, ...] = ()):
    """Reserve une vue aux roles autorises a agir sur l'argent (User.can_act)."""
    return _require(action, lambda user: user.can_act, target_kwarg=target_kwarg, audit_fields=audit_fields)


def require_superadmin(action: str, *, target_kwarg: str = "", audit_fields: tuple[str, ...] = ()):
    """Reserve une vue au superadmin : reglages qui engagent toute la plateforme."""
    return _require(action, lambda user: user.is_superadmin, target_kwarg=target_kwarg, audit_fields=audit_fields)


def _require(action: str, allowed_for, *, target_kwarg: str, audit_fields: tuple[str, ...]):
    """Garde commune a require_acting_role et require_superadmin.

    Ecrit une ligne d'AuditLog AVANT d'executer la vue. Sur cette ligne,
    `allowed` signifie AUTORISE, pas REUSSI :

      - allowed=False : role insuffisant, la vue n'est pas executee, 403 ;
      - allowed=True  : le role permettait la tentative. Rien de plus.

    L'issue d'une tentative autorisee qui echoue (transition interdite,
    formulaire invalide...) est ecrite par la vue dans une SECONDE ligne,
    via audit_failure(). Les deux lignes sont conservees : si la vue
    plante avant d'avoir journalise son resultat, la tentative reste
    tracee.

    `target_kwarg` nomme l'argument d'URL qui identifie la cible.
    `audit_fields` liste les champs POST recopies dans `detail`, sur la
    ligne autorisee comme sur la ligne refusee.
    """

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            target = str(kwargs.get(target_kwarg, ""))
            submitted = {
                name: request.POST[name][:AUDIT_VALUE_MAX_LENGTH] for name in audit_fields if name in request.POST
            }
            if not request.user.is_authenticated or not allowed_for(request.user):
                audit(request, action, target=target, allowed=False, **submitted)
                raise PermissionDenied("Role insuffisant pour cette action")
            audit(request, action, target=target, allowed=True, **submitted)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
