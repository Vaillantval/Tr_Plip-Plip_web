from __future__ import annotations

from rest_framework import authentication, exceptions

from apps.accounts import services as accounts
from apps.accounts.models import Customer


class CustomerTokenAuthentication(authentication.BaseAuthentication):
    """`Authorization: Bearer ppk_...`

    Seule authentification de l'API : une session de la console n'y donne
    aucun acces, et un jeton client n'ouvre pas la console.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != self.keyword.lower().encode():
            return None
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("En-tete Authorization mal forme")
        try:
            raw = header[1].decode()
        except UnicodeError as exc:
            raise exceptions.AuthenticationFailed("Jeton invalide") from exc

        result = accounts.authenticate_token(raw)
        if result is None:
            raise exceptions.AuthenticationFailed("Jeton invalide, expire ou revoque")
        return result

    def authenticate_header(self, request):
        # Donne 401 (et non 403) aux requetes non authentifiees.
        return self.keyword


def is_customer(request) -> bool:
    return isinstance(getattr(request, "user", None), Customer)
