from django.contrib import admin
from django.urls import include, path

from .health import health

urlpatterns = [
    path("health/", health, name="health"),
    # Conserve pour le debug d'urgence, restreint aux superusers.
    # Ce n'est pas la console d'exploitation : voir apps.console.
    path("django-admin/", admin.site.urls),
    path("api/v1/", include("apps.api.urls", namespace="api")),
    path("console/", include("apps.console.urls", namespace="console")),
    path("i18n/", include("django.conf.urls.i18n")),
    # Site client, a la racine.
    path("", include("apps.web.urls", namespace="web")),
]
