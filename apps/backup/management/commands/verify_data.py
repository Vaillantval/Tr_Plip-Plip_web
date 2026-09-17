import json

from django.core.management.base import BaseCommand, CommandError

from apps.backup import crypto, verify


class Command(BaseCommand):
    help = (
        "Verifie l'integrite comptable. Sans argument : la base en place. "
        "Avec un fichier : l'export, sans rien ecrire ni toucher a la base."
    )

    def add_arguments(self, parser):
        parser.add_argument("fichier", nargs="?", help="Export a verifier. Absent : on verifie la base.")

    def handle(self, *args, **options):
        path = options["fichier"]
        if path:
            self.stdout.write(f"Verification du fichier {path}\n")
            results = verify.check_file(self._load(path))
        else:
            self.stdout.write("Verification de la base en place\n")
            results = verify.check_database()

        failed = [r for r in results if not r.ok]
        for result in results:
            mark = "  OK  " if result.ok else " ECHEC"
            style = self.style.SUCCESS if result.ok else self.style.ERROR
            self.stdout.write(style(f"[{mark}] {result.label}") + (f" : {result.detail}" if result.detail else ""))

        if failed:
            raise CommandError(f"\n{len(failed)} controle(s) en echec. Ne pas restaurer ce fichier tel quel.")
        self.stdout.write(self.style.SUCCESS(f"\n{len(results)} controles passes."))

    def _load(self, path):
        try:
            raw = crypto.read_maybe_encrypted(path)
        except (crypto.DecryptionFailed, crypto.EncryptionUnavailable) as exc:
            raise CommandError(str(exc)) from exc
        except OSError as exc:
            raise CommandError(f"Fichier illisible : {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CommandError(
                f"Ce fichier n'est pas un export JSON lisible ({exc}). "
                "S'il est chiffre, son nom doit se terminer par .enc."
            ) from exc
