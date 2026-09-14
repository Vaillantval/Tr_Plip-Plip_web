from __future__ import annotations

from decimal import Decimal

from django.db import transaction as db_transaction

from .models import AccountType, JournalEntry, LedgerAccount, LedgerLine

ZERO = Decimal("0")

FLOAT = "float.plopplop"
CLIENTS_PAYABLE = "clients.payable"
REVENUE_COMMISSION = "revenue.commission"
EXPENSE_FEES = "expense.fees"
CASH_SETTLEMENT = "cash.settlement"

DEFAULT_ACCOUNTS = (
    (FLOAT, "Solde prepaye plopplop", AccountType.ASSET),
    (CLIENTS_PAYABLE, "Fonds clients a reverser", AccountType.LIABILITY),
    (REVENUE_COMMISSION, "Commission Plip-Plip", AccountType.REVENUE),
    (EXPENSE_FEES, "Frais operateurs", AccountType.EXPENSE),
    (CASH_SETTLEMENT, "Contrepartie encaissements", AccountType.ASSET),
)


class UnbalancedEntry(Exception):
    pass


class InvalidTopup(ValueError):
    pass


def ensure_accounts() -> None:
    for code, label, type_ in DEFAULT_ACCOUNTS:
        LedgerAccount.objects.get_or_create(code=code, defaults={"label": label, "type": type_})


@db_transaction.atomic
def post(
    *,
    reference: str,
    description: str,
    lines: list[tuple[str, Decimal, str]],
    transaction=None,
    posted_by=None,
    reverses: JournalEntry | None = None,
) -> JournalEntry:
    """Publie une ecriture equilibree.

    `lines` est une liste de (code_compte, montant_signe, memo).
    Positif = debit, negatif = credit. La somme doit valoir zero.
    """
    total = sum((amount for _, amount, _ in lines), ZERO)
    if total != ZERO:
        raise UnbalancedEntry(f"Ecriture desequilibree de {total} HTG : {reference}")
    if not lines:
        raise UnbalancedEntry("Ecriture sans ligne")

    entry = JournalEntry.objects.create(
        reference=reference,
        description=description,
        transaction=transaction,
        posted_by=posted_by,
        reverses=reverses,
    )
    accounts = {a.code: a for a in LedgerAccount.objects.filter(code__in=[c for c, _, _ in lines])}
    missing = {c for c, _, _ in lines} - set(accounts)
    if missing:
        raise LedgerAccount.DoesNotExist(f"Comptes inconnus : {sorted(missing)}")

    LedgerLine.objects.bulk_create(
        [
            LedgerLine(entry=entry, account=accounts[code], amount=amount, memo=memo)
            for code, amount, memo in lines
        ]
    )
    return entry


@db_transaction.atomic
def reverse(entry: JournalEntry, *, reason: str, posted_by=None) -> JournalEntry:
    """Contre-ecriture. Seule facon de corriger une ecriture publiee."""
    lines = [
        (line.account.code, -line.amount, f"extourne: {line.memo}".strip())
        for line in entry.lines.select_related("account")
    ]
    return post(
        reference=f"{entry.reference}-REV",
        description=f"Extourne — {reason}",
        lines=lines,
        transaction=entry.transaction,
        posted_by=posted_by,
        reverses=entry,
    )


# ----------------------------------------------------------------------
# Ecritures types
# ----------------------------------------------------------------------
def record_payment_received(txn) -> JournalEntry:
    """Encaissement confirme : l'argent entre, et nous devenons debiteurs
    du montant net envers le beneficiaire.
    """
    fees = txn.fee_in + txn.fee_out + txn.fee_platform
    return post(
        reference=txn.reference,
        description="Encaissement confirme",
        transaction=txn,
        lines=[
            (CASH_SETTLEMENT, txn.total_charged, "montant debite au payeur"),
            (CLIENTS_PAYABLE, -txn.net_amount, "du au beneficiaire"),
            (REVENUE_COMMISSION, -fees, "frais et commission encaisses"),
        ],
    )


def record_payout_executed(txn, *, actual_fee: Decimal) -> JournalEntry:
    """Decaissement reussi : la dette envers le client s'eteint, le float
    diminue du net plus les frais reellement factures par l'operateur.
    """
    return post(
        reference=f"{txn.reference}-PAYOUT",
        description="Decaissement execute",
        transaction=txn,
        lines=[
            (CLIENTS_PAYABLE, txn.net_amount, "dette eteinte"),
            (EXPENSE_FEES, actual_fee, "frais operateur reels"),
            (FLOAT, -(txn.net_amount + actual_fee), "sortie de float"),
        ],
    )


def record_refund(txn, *, reason: str, transfer_reference: str, posted_by=None) -> JournalEntry:
    """Remboursement constate : on rend le total debite et on renonce aux frais.

    Le transfert vers le payeur a deja ete fait hors systeme ; sa
    reference figure dans la description, a cote du motif.
    """
    fees = txn.fee_in + txn.fee_out + txn.fee_platform
    return post(
        reference=f"{txn.reference}-REFUND",
        description=f"Remboursement — {reason} — transfert {transfer_reference}",
        transaction=txn,
        posted_by=posted_by,
        lines=[
            (CLIENTS_PAYABLE, txn.net_amount, "dette eteinte par remboursement"),
            (REVENUE_COMMISSION, fees, "frais restitues"),
            (CASH_SETTLEMENT, -txn.total_charged, "montant rendu au payeur"),
        ],
    )


def record_float_topup(amount: Decimal, *, reference: str, posted_by=None) -> JournalEntry:
    """Rechargement du float, constate chez plopplop.

    Un montant negatif viderait le float au grand livre : refuse. La
    reference ne sert qu'une fois, sinon un double envoi du formulaire
    doublerait le rechargement.
    """
    reference = (reference or "").strip()
    if amount is None or amount <= ZERO:
        raise InvalidTopup("Le montant d'un rechargement doit etre strictement positif")
    if not reference:
        raise InvalidTopup("Reference du rechargement obligatoire")
    if JournalEntry.objects.filter(reference=reference).exists():
        raise InvalidTopup(f"Reference deja enregistree au grand livre : {reference}")
    return post(
        reference=reference,
        description="Rechargement du float",
        posted_by=posted_by,
        lines=[
            (FLOAT, amount, "rechargement"),
            (CASH_SETTLEMENT, -amount, "sortie tresorerie"),
        ],
    )
