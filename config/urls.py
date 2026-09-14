from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    # Conserve pour le debug d'urgence, restreint aux superusers.
    # Ce n'est pas la console d'exploitation : voir apps.console.
    path("django-admin/", admin.site.urls),
    path("", include("apps.console.urls", namespace="console")),
]
