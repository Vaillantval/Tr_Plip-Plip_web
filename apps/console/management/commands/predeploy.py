"""Etape de pre-deploiement (Railway : preDeployCommand).

Execute dans un conteneur separe, avant la mise en service. Tout echec
annule le deploiement : mieux vaut garder l'ancienne version que servir
une configuration de production incomplete.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Verifications de production, migrations, comptes du grand livre et superadmin."

    def handle(self, *args, **options):
        self.stdout.write("1/4 check --deploy")
        call_command("check", deploy=True, fail_level="ERROR")
        self.stdout.write("2/4 migrate")
        call_command("migrate", interactive=False)
        self.stdout.write("3/4 init_ledger")
        call_command("init_ledger")
        self.stdout.write("4/4 init_superadmin")
        call_command("init_superadmin")
        self.stdout.write(self.style.SUCCESS("Pre-deploiement termine."))
