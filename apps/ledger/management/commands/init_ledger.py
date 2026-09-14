from django.core.management.base import BaseCommand

from apps.ledger.services import ensure_accounts


class Command(BaseCommand):
    help = "Cree les comptes du plan comptable interne."

    def handle(self, *args, **options):
        ensure_accounts()
        self.stdout.write(self.style.SUCCESS("Comptes du grand livre en place."))
