"""Construction des deux fichiers d'export.

Deux fichiers parce qu'ils n'ont pas la meme sensibilite : le grand livre
peut se partager pour analyse, les identifiants operateurs jamais.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.core import serializers

from . import crypto, schema

#: Nom des fichiers. L'horodatage est celui de l'export, en UTC : deux
#: sauvegardes prises dans deux fuseaux se rangent quand meme dans
#: l'ordre ou elles ont ete faites.
STEM = "plipplip-%Y%m%d-%H%M%S"
OPERATOR_SUFFIX = "-operateurs"


def _git_revision() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent.parent.parent,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""


def _rows(label: str) -> list[dict]:
    model = schema.get_model(label)
    # order_by("pk") : un export rejoue deux fois donne le meme fichier,
    # ce qui rend un `diff` entre deux sauvegardes lisible.
    return json.loads(serializers.serialize("json", model.objects.order_by("pk").iterator()))


def build(labels, *, taken_at=None) -> dict:
    """Enveloppe complete : en-tete, puis les tables dans l'ordre d'import."""
    tables = {label: _rows(label) for label in labels}
    return {
        "meta": {
            "format": schema.FORMAT_VERSION,
            "genere_le": (taken_at or datetime.now(dt_timezone.utc)).isoformat(),
            "revision_git": _git_revision(),
            # Relu par verify_data : un en-tete qui ment sur les comptes
            # signale un fichier tronque a l'ecriture.
            "comptes": {label: len(rows) for label, rows in tables.items()},
        },
        "tables": tables,
    }


def dumps(labels, *, taken_at=None) -> str:
    return json.dumps(build(labels, taken_at=taken_at), ensure_ascii=False, indent=1)


def write(
    directory,
    *,
    encrypt_main: bool = False,
    passphrase: str | None = None,
    taken_at=None,
) -> dict:
    """Ecrit les deux fichiers. Renvoie leurs chemins et leurs tailles.

    Le fichier operateurs est chiffre DANS TOUS LES CAS : `encrypt_main`
    ne le concerne pas.
    """
    taken_at = taken_at or datetime.now(dt_timezone.utc)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = taken_at.strftime(STEM)

    # La phrase est demandee AVANT d'ecrire quoi que ce soit : mieux vaut
    # echouer sur un dossier vide que sur une moitie de sauvegarde.
    if passphrase is None:
        passphrase = crypto.get_passphrase(confirm=True)

    main = directory / f"{stem}.json"
    payload = dumps(schema.TABLES, taken_at=taken_at)
    if encrypt_main:
        main = main.with_name(main.name + crypto.SUFFIX)
        crypto.encrypt(payload, main, passphrase)
    else:
        main.write_text(payload, encoding="utf-8")

    operators = directory / f"{stem}{OPERATOR_SUFFIX}.json{crypto.SUFFIX}"
    crypto.encrypt(dumps(schema.OPERATOR_TABLES, taken_at=taken_at), operators, passphrase)

    return {
        "principal": main,
        "operateurs": operators,
        "tailles": {main.name: main.stat().st_size, operators.name: operators.stat().st_size},
        "comptes": {label: len(rows) for label, rows in json.loads(payload)["tables"].items()},
    }
