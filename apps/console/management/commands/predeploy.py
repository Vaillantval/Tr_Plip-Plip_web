"""Etape de pre-deploiement (Railway : preDeployCommand).

Execute dans un conteneur separe, avant la mise en service. Tout echec
annule le deploiement : mieux vaut garder l'ancienne version que servir
une configuration de production incomplete.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Verifications de production, migrations et comptes du grand livre."

    def handle(self, *args, **options):
        self.stdout.write("1/3 check --deploy")
        call_command("check", deploy=True, fail_level="ERROR")
        self.stdout.write("2/3 migrate")
        call_command("migrate", interactive=False)
        self.stdout.write("3/3 init_ledger")
        call_command("init_ledger")
        self.stdout.write(self.style.SUCCESS("Pre-deploiement termine."))
