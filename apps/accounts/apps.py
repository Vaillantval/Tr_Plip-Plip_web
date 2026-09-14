from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    label = "accounts"

    def ready(self):
        from . import checks  # noqa: F401  enregistre les checks de deploiement
