"""Langue de la requete : creole par defaut sur le site client.

Remplace django.middleware.locale.LocaleMiddleware, qui suit l'en-tete
Accept-Language du navigateur : en Haiti, presque tous les telephones
annoncent le francais ou l'anglais, et le client ne verrait jamais le
creole sans le chercher. Ici, seul le choix explicite du client (le
cookie pose par le selecteur de langue) l'emporte sur le creole.

La console et l'admin Django restent en francais, quel que soit le cookie.
"""

from __future__ import annotations

from django.conf import settings
from django.utils import translation
from django.utils.cache import patch_vary_headers

#: Prefixes servis en francais, toujours.
FRENCH_ONLY = ("/console/", "/django-admin/")


def language_for_request(request) -> str:
    if request.path_info.startswith(FRENCH_ONLY):
        return "fr"
    chosen = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME, "")
    if chosen in dict(settings.LANGUAGES):
        return chosen
    return settings.SITE_DEFAULT_LANGUAGE


class SiteLanguageMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        language = language_for_request(request)
        translation.activate(language)
        request.LANGUAGE_CODE = language
        response = self.get_response(request)
        patch_vary_headers(response, ("Cookie",))
        response.headers.setdefault("Content-Language", language)
        return response
