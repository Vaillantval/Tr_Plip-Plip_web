from __future__ import annotations

from rest_framework.permissions import BasePermission

from .authentication import is_customer


class IsCustomer(BasePermission):
    message = "Authentification client requise"

    def has_permission(self, request, view):
        return is_customer(request)
