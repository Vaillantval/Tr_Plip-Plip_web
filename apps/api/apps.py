from django.apps import AppConfig


class ApiConfig(AppConfig):
    name = "apps.api"
    label = "api"

    def ready(self):
        from . import schema  # noqa: F401  extension OpenAPI de l'authentification
