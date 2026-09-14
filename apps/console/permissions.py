"""Garde-fous de la console.

Toute action qui touche a l'argent passe par require_acting_role. Les
refus sont journalises au meme titre que les acceptations : une
tentative refusee est une information d'exploitation.
"""

from __future__ import annotations

from functools import wraps

from django.core.exceptions import PermissionDenied

from apps.accounts.models import AuditLog


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


def require_acting_role(action: str):
    """Reserve une vue aux roles autorises a agir sur l'argent."""

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            target = kwargs.get("reference", "")
            if not request.user.is_authenticated or not request.user.can_act:
                audit(request, action, target=target, allowed=False)
                raise PermissionDenied("Role insuffisant pour cette action")
            audit(request, action, target=target, allowed=True)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
