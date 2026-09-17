from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.backup import crypto
from apps.backup.export import write

DEFAULT_DIR = Path(settings.BASE_DIR) / "sauvegardes"


class Command(BaseCommand):
    help = (
        "Ecrit une sauvegarde en deux fichiers : le corps (grand livre, transferts, "
        "reglages, clients) et les comptes operateurs, toujours chiffres."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output", default=str(DEFAULT_DIR), help="Dossier de destination.")
        parser.add_argument(
            "--encrypt",
            action="store_true",
            help="Chiffre aussi le fichier principal. Le fichier operateurs l'est toujours.",
        )

    def handle(self, *args, **options):
        try:
            result = write(options["output"], encrypt_main=options["encrypt"])
        except crypto.EncryptionUnavailable as exc:
            raise CommandError(str(exc)) from exc

        for label, count in result["comptes"].items():
            self.stdout.write(f"  {label:<38} {count:>7}")
        self.stdout.write("")
        for name, size in result["tailles"].items():
            self.stdout.write(f"  {name}  ({size / 1024:.1f} Kio)")
        self.stdout.write(self.style.SUCCESS(f"\nSauvegarde ecrite dans {result['principal'].parent}"))
        self.stdout.write(
            self.style.WARNING(
                "Le second exemplaire doit vivre AILLEURS QUE CHEZ RAILWAY : une sauvegarde\n"
                "chez l'hebergeur ne protege pas de la perte de l'hebergeur.\n"
                "Ce fichier contient des numeros de telephone et des montants : aucun stockage\n"
                "public, aucun envoi par e-mail non chiffre. Voir RESTAURATION.md."
            )
        )
