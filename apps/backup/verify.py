"""Verification d'integrite, sur un FICHIER ou sur la BASE.

Le mode fichier est celui qui compte. Il repond a « ce fichier est-il
restaurable ? » AVANT qu'on touche a quoi que ce soit : un controle qui
ne sait lire que la base ne valide l'export qu'une fois importe,
c'est-a-dire trop tard.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from . import schema

ZERO = Decimal("0")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class Result:
    label: str
    ok: bool
    detail: str = ""


def _cents(total) -> Decimal:
    """Somme d'agregat ramenee au centime.

    Meme raison que apps/ledger/models.py:_sum_to_cents : SQLite additionne
    les DecimalField en virgule flottante, une ecriture equilibree peut
    sommer a -9e-14. Les montants ayant deux decimales, l'arrondi est exact.
    """
    return Decimal(total or 0).quantize(CENT)


# ----------------------------------------------------------------------
# Sur le fichier : aucune base de donnees, aucune ecriture
# ----------------------------------------------------------------------
def check_file(payload: dict) -> list[Result]:
    results = [_format_version(payload)]
    if not results[0].ok:
        # Un format inconnu rend tout le reste ininterpretable.
        return results
    tables = payload.get("tables", {})
    results.append(_header_matches(payload, tables))
    results.append(_entries_balance(tables))
    results.append(_global_sum(tables))
    results.extend(_referential_integrity(tables))
    return results


def _format_version(payload: dict) -> Result:
    version = payload.get("meta", {}).get("format")
    if version == schema.FORMAT_VERSION:
        return Result("Format du fichier", True, f"version {version}")
    return Result(
        "Format du fichier",
        False,
        f"version {version!r}, attendu {schema.FORMAT_VERSION}. "
        "Fichier ecrit par une autre version du code, ou fichier tronque.",
    )


def _header_matches(payload: dict, tables: dict) -> Result:
    announced = payload.get("meta", {}).get("comptes", {})
    mismatched = [
        f"{label} : en-tete {count}, trouve {len(tables.get(label, []))}"
        for label, count in announced.items()
        if len(tables.get(label, [])) != count
    ]
    missing = [label for label in announced if label not in tables]
    if mismatched or missing:
        detail = " ; ".join(mismatched + [f"table absente : {m}" for m in missing])
        return Result("En-tete", False, f"{detail}. Fichier probablement tronque a l'ecriture.")
    total = sum(len(rows) for rows in tables.values())
    return Result("En-tete", True, f"{len(tables)} tables, {total} lignes, comptes conformes")


def _lines_by_entry(tables: dict) -> dict:
    grouped = defaultdict(list)
    for row in tables.get("ledger.ledgerline", []):
        grouped[row["fields"]["entry"]].append(Decimal(row["fields"]["amount"]))
    return grouped


def _entries_balance(tables: dict) -> Result:
    grouped = _lines_by_entry(tables)
    unbalanced, empty = [], []
    for row in tables.get("ledger.journalentry", []):
        lines = grouped.get(row["pk"])
        if not lines:
            empty.append(str(row["fields"].get("reference") or row["pk"]))
        elif _cents(sum(lines, ZERO)) != ZERO:
            unbalanced.append(f"{row['fields'].get('reference') or row['pk']} ({_cents(sum(lines, ZERO))})")
    if unbalanced or empty:
        parts = []
        if unbalanced:
            parts.append(f"desequilibrees : {', '.join(unbalanced[:10])}")
        if empty:
            parts.append(f"sans ligne : {', '.join(empty[:10])}")
        return Result("Equilibre de chaque ecriture", False, " ; ".join(parts))
    return Result("Equilibre de chaque ecriture", True, f"{len(tables.get('ledger.journalentry', []))} ecritures")


def _global_sum(tables: dict) -> Result:
    total = _cents(sum((Decimal(r["fields"]["amount"]) for r in tables.get("ledger.ledgerline", [])), ZERO))
    if total != ZERO:
        return Result("Somme globale des lignes", False, f"{total} HTG au lieu de 0")
    return Result("Somme globale des lignes", True, f"{len(tables.get('ledger.ledgerline', []))} lignes, somme nulle")


def _referential_integrity(tables: dict) -> list[Result]:
    known = {label: {row["pk"] for row in rows} for label, rows in tables.items()}
    results = []
    for label, keys in schema.FOREIGN_KEYS.items():
        dangling = []
        for row in tables.get(label, []):
            for field, target in keys.items():
                value = row["fields"].get(field)
                if value is not None and value not in known.get(target, set()):
                    dangling.append(f"{label}#{row['pk']}.{field} -> {target}#{value}")
        if dangling:
            results.append(
                Result(
                    f"Liens internes de {label}",
                    False,
                    f"{len(dangling)} lien(s) dans le vide : {', '.join(dangling[:5])}",
                )
            )
    if not results:
        counted = sum(len(k) for k in schema.FOREIGN_KEYS.values())
        return [Result("Liens internes du fichier", True, f"{counted} cles etrangeres verifiees")]
    return results


# ----------------------------------------------------------------------
# Sur la base
# ----------------------------------------------------------------------
def check_database() -> list[Result]:
    from django.db.models import Count, Sum

    from apps.ledger.models import JournalEntry, LedgerAccount, LedgerLine
    from apps.ledger.services import CLIENTS_PAYABLE, FLOAT
    from apps.transactions.models import Transaction
    from apps.treasury.models import FloatSnapshot
    from apps.treasury.services import outstanding_liability

    results = []

    unbalanced = [e.reference for e in JournalEntry.objects.prefetch_related("lines") if not e.is_balanced()]
    results.append(
        Result("Equilibre de chaque ecriture", not unbalanced, ", ".join(unbalanced[:10]) or f"{JournalEntry.objects.count()} ecritures")
    )

    total = _cents(LedgerLine.objects.aggregate(t=Sum("amount"))["t"])
    results.append(Result("Somme globale des lignes", total == ZERO, f"{total} HTG"))

    orphans = list(
        Transaction.objects.filter(payment_confirmed_at__isnull=False, journal_entries__isnull=True).values_list(
            "reference", flat=True
        )[:10]
    )
    results.append(
        Result(
            "Transferts encaisses sans ecriture",
            not orphans,
            ", ".join(orphans) or "aucun",
        )
    )

    balances = [f"{a.code} : {a.balance()}" for a in LedgerAccount.objects.all()]
    results.append(Result("Soldes par compte", True, "\n    ".join([""] + balances)))

    payable = LedgerAccount.objects.filter(code=CLIENTS_PAYABLE).first()
    liability = outstanding_liability()
    payable_balance = payable.balance() if payable else ZERO
    results.append(
        Result(
            "Dette clients au grand livre",
            _cents(payable_balance) == _cents(-liability),
            f"{CLIENTS_PAYABLE} = {payable_balance}, transferts en cours = {liability}",
        )
    )

    duplicates = [
        row["reference"]
        for row in JournalEntry.objects.values("reference").annotate(n=Count("id")).filter(n__gt=1)
    ]
    results.append(Result("References d'ecriture en double", not duplicates, ", ".join(duplicates[:10]) or "aucune"))

    snapshot = FloatSnapshot.objects.first()
    if snapshot is None:
        results.append(Result("Derive du float", True, "aucun releve : rien a comparer"))
    else:
        float_account = LedgerAccount.objects.filter(code=FLOAT).first()
        drift = _cents((float_account.balance() if float_account else ZERO) - snapshot.provider_balance)
        results.append(
            Result("Derive du float", drift == ZERO, f"{drift} HTG face au releve du {snapshot.created_at:%Y-%m-%d %H:%M}")
        )

    return results
