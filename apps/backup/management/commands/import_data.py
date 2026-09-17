import json

from django.core.management.base import BaseCommand, CommandError

from apps.backup import crypto, restore, verify


class Command(BaseCommand):
    help = (
        "Restaure un export. Refuse une base non vide sans --force. "
        "Verifie automatiquement le resultat : un import qui ne verifie pas ne prouve rien."
    )

    def add_arguments(self, parser):
        parser.add_argument("fichier", help="Export principal (clair ou .enc).")
        parser.add_argument(
            "--operateurs",
            help="Fichier des comptes operateurs. Absent : les comptes ne sont pas restaures "
            "et les liens « qui a fait quoi » sont vides.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="VIDE la base avant de restaurer. Sans ce drapeau, une base non vide est refusee.",
        )

    def handle(self, *args, **options):
        payload = self._load(options["fichier"])
        problems = [r for r in verify.check_file(payload) if not r.ok]
        if problems:
            for result in problems:
                self.stdout.write(self.style.ERROR(f"[ ECHEC] {result.label} : {result.detail}"))
            raise CommandError("Ce fichier ne passe pas la verification. Rien n'a ete importe.")
        self.stdout.write(self.style.SUCCESS("Fichier verifie avant import."))

        operators = self._load(options["operateurs"]) if options["operateurs"] else None
        if operators is None:
            self.stdout.write(
                self.style.WARNING(
                    "Sans --operateurs : aucun compte operateur restaure. Les traces « qui a fait quoi »\n"
                    "perdent leur auteur, et seul le superadmin recree par init_superadmin pourra se\n"
                    "connecter. L'argent, lui, est complet."
                )
            )

        existing = restore.database_contents()
        if existing and not options["force"]:
            listing = ", ".join(f"{label} : {count}" for label, count in existing.items())
            raise CommandError(
                f"La base n'est pas vide ({listing}).\n"
                "Rien n'a ete importe. Relancer avec --force pour la VIDER puis restaurer."
            )

        report = restore.restore(payload, operators_payload=operators, wipe=options["force"])

        for label, count in report["restaure"].items():
            self.stdout.write(f"  {label:<38} {count:>7}")
        if report["operateurs"]:
            self.stdout.write(f"  accounts.user (operateurs)             {report['operateurs']:>7}")
        self.stdout.write(f"\nSequences recalees : {report['sequences']} instruction(s).")

        self.stdout.write("\nVerification de la base restauree :")
        failed = [r for r in verify.check_database() if not r.ok]
        for result in failed:
            self.stdout.write(self.style.ERROR(f"[ ECHEC] {result.label} : {result.detail}"))
        if failed:
            raise CommandError("La base restauree ne passe pas la verification. NE PAS remettre en service.")

        self.stdout.write(self.style.SUCCESS("\nRestauration verifiee."))
        self.stdout.write(
            self.style.WARNING(
                "Deux choses a faire maintenant, dans cet ordre (RESTAURATION.md, etapes 5 et 6) :\n"
                "  1. faire changer les mots de passe operateurs -- un export restaure est un\n"
                "     export qui a circule ;\n"
                "  2. prevenir que les clients devront se reconnecter par SMS : les jetons de\n"
                "     session ne sont jamais sauvegardes. C'est normal, ce n'est pas une panne."
            )
        )

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
            raise CommandError(f"Ce fichier n'est pas un export JSON lisible ({exc}).") from exc
