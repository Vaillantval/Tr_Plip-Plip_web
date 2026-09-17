"""Restauration d'un export.

Deux contraintes gouvernent tout ce fichier.

1. `loaddata` est INUTILISABLE ici. Le deserialiseur Django pose la cle
   primaire puis appelle save(), et les quatre gardes append-only
   (JournalEntry, LedgerLine, TransactionEvent, AuditLog) levent
   RuntimeError des que self.pk n'est pas None. On passe donc par
   bulk_create, qui ne passe pas par save() -- c'est deja le chemin
   qu'emprunte ledger.post() pour ses lignes.

2. Les cles primaires sont restaurees A L'IDENTIQUE. Si une seule bouge,
   LedgerLine.entry_id, TransactionEvent.transaction_id et
   JournalEntry.transaction_id pointent ailleurs : le grand livre reste
   equilibre compte par compte, donc muet, mais les ecritures sont
   rattachees aux mauvaises transactions. Une restauration silencieusement
   fausse est pire qu'une restauration qui echoue.
"""

from __future__ import annotations

from django.core import serializers
from django.core.management.color import no_style
from django.db import connection, transaction as db_transaction

from . import schema

#: Ordre de SUPPRESSION : l'inverse des dependances, precede de ce qui
#: n'est pas sauvegarde mais protege quand meme une transaction.
#: notifications.Notification est en PROTECT sur Transaction : sans cette
#: ligne, vider la base echoue sur un ProtectedError incomprehensible.
WIPE_FIRST = ("notifications.notification", "accounts.customertoken")

BATCH = 500


class NonEmptyDatabase(RuntimeError):
    pass


def database_contents() -> dict:
    """Ce que la base contient deja, pour le dire avant de l'ecraser."""
    counts = {}
    for label in schema.TABLES:
        count = schema.get_model(label).objects.count()
        if count:
            counts[label] = count
    return counts


def _prepare(label: str, rows: list[dict], *, keep_operators: bool, known_users: set) -> list[dict]:
    """Trie par cle primaire et traite les liens vers les operateurs.

    Le tri importe pour JournalEntry.reverses, qui pointe vers une autre
    ecriture de la meme table : une contre-ecriture porte toujours une cle
    plus haute que ce qu'elle annule, donc l'ordre croissant suffit.
    """
    rows = sorted(rows, key=lambda row: row["pk"])
    fields = schema.OPERATOR_KEYS.get(label, ())
    if not fields:
        return rows
    for row in rows:
        for field in fields:
            value = row["fields"].get(field)
            if value is not None and (not keep_operators or value not in known_users):
                # Tous ces liens sont en SET_NULL : on perd « qui a fait
                # quoi », jamais l'argent.
                row["fields"][field] = None
    return rows


def _insert(label: str, rows: list[dict]) -> int:
    model = schema.get_model(label)
    objects = [item.object for item in serializers.deserialize("python", rows)]
    model.objects.bulk_create(objects, batch_size=BATCH)
    return len(objects)


def reset_sequences(labels) -> int:
    """Recale les sequences Postgres au maximum des cles inserees.

    Sans cela, la premiere ecriture apres restauration reclame une cle
    deja prise et echoue. Sous SQLite, Django ne renvoie aucune requete :
    la table sqlite_sequence suit deja le maximum insere.
    """
    models = [schema.get_model(label) for label in labels]
    statements = connection.ops.sequence_reset_sql(no_style(), models)
    if not statements:
        return 0
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    return len(statements)


@db_transaction.atomic
def restore(payload: dict, *, operators_payload: dict | None = None, wipe: bool = False) -> dict:
    from apps.ledger.services import ensure_accounts

    tables = payload["tables"]
    report = {"supprime": {}, "restaure": {}, "operateurs": 0, "sequences": 0}

    if wipe:
        for label in WIPE_FIRST + tuple(reversed(schema.TABLES)):
            deleted, _ = schema.get_model(label).objects.all().delete()
            if deleted:
                report["supprime"][label] = deleted
        if operators_payload is not None:
            deleted, _ = schema.get_model("accounts.user").objects.all().delete()
            report["supprime"]["accounts.user"] = deleted

    known_users: set = set()
    if operators_payload is not None:
        for label in schema.OPERATOR_TABLES:
            rows = sorted(operators_payload["tables"].get(label, []), key=lambda row: row["pk"])
            known_users |= {row["pk"] for row in rows}
            report["operateurs"] += _insert(label, rows)

    # Les comptes du grand livre viennent du fichier quand il en a :
    # ensure_accounts() leur donnerait des cles neuves, et toutes les
    # lignes de l'export pointeraient dans le vide.
    for label in schema.TABLES:
        rows = _prepare(
            label,
            tables.get(label, []),
            keep_operators=operators_payload is not None,
            known_users=known_users,
        )
        if rows:
            report["restaure"][label] = _insert(label, rows)

    # Un code de compte ajoute au projet depuis cette sauvegarde manquerait
    # sinon a l'appel : get_or_create ne touche pas a ceux du fichier.
    ensure_accounts()

    report["sequences"] = reset_sequences(list(schema.TABLES) + list(schema.OPERATOR_TABLES))
    return report
