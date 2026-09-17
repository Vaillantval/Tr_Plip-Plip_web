"""Chiffrement des sauvegardes, delegue a openssl.

Le choix se justifie par le DECHIFFREMENT, pas par le chiffrement : la
commande inverse tient sur une ligne et ne demande ni Python, ni Django,
ni ce depot. Le jour ou l'on restaure, l'application est peut-etre
justement ce qui ne demarre pas. Elle est ecrite dans RESTAURATION.md,
et elle a ete executee dans les deux sens avant d'y etre ecrite.

Le clair ne touche JAMAIS le disque : openssl lit sur son entree
standard et ecrit directement le fichier chiffre.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess

#: Nom de la variable d'environnement qui porte la phrase secrete.
#: Jamais en argument de ligne de commande : un argument est visible dans
#: `ps` et reste dans l'historique du shell.
PASSPHRASE_ENV = "PLIPPLIP_BACKUP_PASSPHRASE"

#: Doit correspondre EXACTEMENT a ce qu'annonce RESTAURATION.md.
CIPHER = ("-aes-256-cbc", "-pbkdf2", "-iter", "600000")

SUFFIX = ".enc"


class EncryptionUnavailable(RuntimeError):
    """openssl introuvable. On refuse d'ecrire plutot que d'ecrire en clair."""


class DecryptionFailed(RuntimeError):
    """Mauvaise phrase secrete, ou fichier abime."""


def openssl_path() -> str:
    path = shutil.which("openssl")
    if not path:
        raise EncryptionUnavailable(
            "openssl est introuvable. Rien n'a ete ecrit : un fichier de comptes "
            "operateurs en clair serait pire que pas de sauvegarde du tout."
        )
    return path


def available() -> bool:
    return shutil.which("openssl") is not None


def get_passphrase(*, confirm: bool = False) -> str:
    """Phrase secrete : environnement d'abord, sinon saisie masquee."""
    value = os.environ.get(PASSPHRASE_ENV, "")
    if value:
        return value
    value = getpass.getpass("Phrase secrete de la sauvegarde : ")
    if not value:
        raise EncryptionUnavailable("Phrase secrete vide : rien n'a ete ecrit.")
    if confirm and value != getpass.getpass("Confirmer la phrase secrete : "):
        raise EncryptionUnavailable("Les deux phrases different : rien n'a ete ecrit.")
    return value


def _env(passphrase: str) -> dict:
    child = os.environ.copy()
    child[PASSPHRASE_ENV] = passphrase
    return child


def encrypt(data: str, destination, passphrase: str) -> None:
    """Ecrit `data` chiffre dans `destination`. Le clair reste en memoire."""
    result = subprocess.run(
        [openssl_path(), "enc", *CIPHER, "-salt", "-out", str(destination), "-pass", f"env:{PASSPHRASE_ENV}"],
        input=data.encode("utf-8"),
        capture_output=True,
        env=_env(passphrase),
    )
    if result.returncode != 0:
        # Ne jamais laisser derriere soi un fichier chiffre a moitie : on
        # le relirait un jour en croyant qu'il vaut quelque chose.
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise EncryptionUnavailable(f"Chiffrement echoue : {result.stderr.decode('utf-8', 'replace').strip()}")


def decrypt(source, passphrase: str) -> str:
    """Renvoie le clair d'un fichier chiffre, sans passer par le disque."""
    result = subprocess.run(
        [openssl_path(), "enc", "-d", *CIPHER, "-in", str(source), "-pass", f"env:{PASSPHRASE_ENV}"],
        capture_output=True,
        env=_env(passphrase),
    )
    if result.returncode != 0:
        raise DecryptionFailed(
            "Dechiffrement impossible : phrase secrete incorrecte, ou fichier abime. "
            f"({result.stderr.decode('utf-8', 'replace').strip().splitlines()[0] if result.stderr else 'sans detail'})"
        )
    return result.stdout.decode("utf-8")


def read_maybe_encrypted(path, passphrase_getter=get_passphrase) -> str:
    """Lit un export, qu'il soit chiffre ou non. Le suffixe decide."""
    if str(path).endswith(SUFFIX):
        return decrypt(path, passphrase_getter())
    with open(path, encoding="utf-8") as handle:
        return handle.read()
