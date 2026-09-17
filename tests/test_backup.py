"""Sauvegarde et restauration.

Ce qu'on protege ici n'est pas « le fichier existe », c'est « la base
restauree dit la meme chose que celle qu'on a perdue ». Deux pieges
dominent :

  - une restauration silencieusement fausse, ou les soldes tombent juste
    mais les ecritures sont rattachees aux mauvaises transactions ;
  - la disparition des cles d'idempotence, qui transforme chaque demande
    rejouee en second transfert reel.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.accounts.models import AuditLog, Customer, CustomerToken, Role, User
from apps.api.models import IdempotencyKey
from apps.backup import crypto, restore, schema, verify
from apps.backup.export import build, dumps, write
from apps.ledger import services as ledger
from apps.ledger.models import JournalEntry, LedgerAccount, LedgerLine
from apps.providers.plopplop.client import WithdrawalResult
from apps.transactions import services
from apps.transactions.models import Transaction, TransactionEvent, Wallet
from apps.transactions.states import State
from apps.treasury import services as treasury

PASSPHRASE = "phrase de test"
GET_CLIENT = "apps.transactions.services.get_client"


# ----------------------------------------------------------------------
# Un cycle de vie complet : c'est lui qu'on sauvegarde et qu'on restaure
# ----------------------------------------------------------------------
@pytest.fixture
def books(db):
    ledger.ensure_accounts()


@pytest.fixture
def livre(books):
    """Float recharge, un transfert termine, un transfert rembourse."""
    operator = User.objects.create_user(username="operateur", password="x" * 12, role=Role.OPERATOR)
    ledger.record_float_topup(Decimal("10000"), reference="TOPUP-1", posted_by=operator)

    alice = Customer.objects.create(phone="50937000001", language="ht")
    bob = Customer.objects.create(phone="50937000002")
    # Un jeton de session vivant : il ne doit se retrouver dans AUCUN fichier.
    CustomerToken.objects.create(
        customer=alice, key_hash="a" * 64, prefix="abc", expires_at=timezone.now() + timedelta(days=1)
    )

    done = _complete(alice, operator)
    refunded = _refund(bob, operator)

    IdempotencyKey.objects.create(customer=alice, key="cle-alice", request_fingerprint="f" * 64, transaction=done)
    IdempotencyKey.objects.create(customer=bob, key="cle-bob", request_fingerprint="e" * 64, transaction=refunded)

    AuditLog.objects.create(user=operator, action="transaction.refund", target=refunded.reference, allowed=True)
    treasury.record_snapshot(provider_balance=LedgerAccount.objects.get(code=ledger.FLOAT).balance())
    return {"operator": operator, "alice": alice, "bob": bob, "done": done, "refunded": refunded}


def _make(customer) -> Transaction:
    return services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone="50932123456",
        sender_phone=customer.phone,
        net_amount=Decimal("1000"),
        customer=customer,
    )


def _complete(customer, operator) -> Transaction:
    txn = _make(customer)
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()
    result = WithdrawalResult(
        transaction_id="API_WD_NA_1",
        api_reference="9876543210",
        reference=txn.build_payout_reference(),
        amount=Decimal("1000"),
        fee=Decimal("25"),
        total=Decimal("1025"),
        balance_after=Decimal("8975"),
        status="success",
    )
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.return_value = result
        services.execute_payout(txn)
    txn.refresh_from_db()
    assert txn.state == State.COMPLETED
    return txn


def _refund(customer, operator) -> Transaction:
    txn = _make(customer)
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()
    services.refund(txn, reason="beneficiaire injoignable", transfer_reference="MAN-77", actor=operator)
    txn.refresh_from_db()
    assert txn.state == State.REFUNDED
    return txn


def _snapshot() -> dict:
    """Etat comptable observable, pour comparer avant et apres."""
    return {
        "soldes": {a.code: a.balance() for a in LedgerAccount.objects.all()},
        "ecritures": sorted(JournalEntry.objects.values_list("reference", flat=True)),
        "transferts": sorted(Transaction.objects.values_list("reference", "state")),
        "rattachements": {
            txn.reference: (
                sorted(txn.journal_entries.values_list("reference", flat=True)),
                sorted(txn.events.values_list("from_state", "to_state")),
            )
            for txn in Transaction.objects.all()
        },
    }


def _roundtrip(tmp_path, *, operators=True) -> dict:
    """Exporte, vide tout, reimporte. Renvoie l'etat d'avant."""
    before = _snapshot()
    paths = write(tmp_path, passphrase=PASSPHRASE)
    payload = json.loads(paths["principal"].read_text(encoding="utf-8"))
    ops = json.loads(crypto.decrypt(paths["operateurs"], PASSPHRASE)) if operators else None
    restore.restore(payload, operators_payload=ops, wipe=True)
    return before


# ----------------------------------------------------------------------
# L'aller-retour
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_export_then_import_restores_every_balance(livre, tmp_path):
    before = _roundtrip(tmp_path)
    assert _snapshot()["soldes"] == before["soldes"]
    assert all(entry.is_balanced() for entry in JournalEntry.objects.all())


@pytest.mark.django_db
def test_each_transaction_keeps_its_own_entries_and_events(livre, tmp_path):
    """Le seul test qui attrape une restauration silencieusement fausse.

    Des cles primaires reattribuees laissent les soldes justes compte par
    compte -- donc muets -- tout en rattachant les ecritures aux mauvaises
    transactions. On compare le rattachement, pas les totaux.
    """
    before = _roundtrip(tmp_path)
    after = _snapshot()
    assert after["rattachements"] == before["rattachements"]
    # Et pas seulement en nombre : l'ecriture de decaissement du transfert
    # termine ne doit pas s'etre glissee sous le transfert rembourse.
    done = Transaction.objects.get(reference=livre["done"].reference)
    assert f"{done.reference}-PAYOUT" in set(done.journal_entries.values_list("reference", flat=True))
    refunded = Transaction.objects.get(reference=livre["refunded"].reference)
    assert f"{refunded.reference}-REFUND" in set(refunded.journal_entries.values_list("reference", flat=True))


@pytest.mark.django_db
def test_primary_keys_are_restored_unchanged(livre, tmp_path):
    before = {
        "transactions": dict(Transaction.objects.values_list("pk", "reference")),
        "ecritures": dict(JournalEntry.objects.values_list("pk", "reference")),
        "lignes": sorted(LedgerLine.objects.values_list("pk", "entry_id", "account_id")),
        "evenements": sorted(TransactionEvent.objects.values_list("pk", "transaction_id")),
    }
    _roundtrip(tmp_path)
    assert dict(Transaction.objects.values_list("pk", "reference")) == before["transactions"]
    assert dict(JournalEntry.objects.values_list("pk", "reference")) == before["ecritures"]
    assert sorted(LedgerLine.objects.values_list("pk", "entry_id", "account_id")) == before["lignes"]
    assert sorted(TransactionEvent.objects.values_list("pk", "transaction_id")) == before["evenements"]


@pytest.mark.django_db
def test_the_first_write_after_a_restore_does_not_collide(livre, tmp_path):
    """Sequences recalees : sans cela, la premiere ecriture reclame une cle prise."""
    _roundtrip(tmp_path)
    highest = JournalEntry.objects.order_by("-pk").first().pk
    entry = ledger.record_float_topup(Decimal("500"), reference="TOPUP-APRES")
    assert entry.pk > highest
    assert JournalEntry.objects.filter(reference="TOPUP-APRES").count() == 1


@pytest.mark.django_db
def test_idempotency_keys_survive_the_round_trip(livre, tmp_path):
    """Sans elles, une demande rejouee cree un SECOND transfert reel."""
    before = sorted(IdempotencyKey.objects.values_list("customer__phone", "key", "transaction__reference"))
    _roundtrip(tmp_path)
    assert sorted(IdempotencyKey.objects.values_list("customer__phone", "key", "transaction__reference")) == before
    assert IdempotencyKey.objects.count() == 2


@pytest.mark.django_db
def test_restored_ledger_keeps_its_entry_references(livre, tmp_path):
    before = _roundtrip(tmp_path)
    assert _snapshot()["ecritures"] == before["ecritures"]
    assert any(r.endswith("-PAYOUT") for r in before["ecritures"])
    assert any(r.endswith("-REFUND") for r in before["ecritures"])


@pytest.mark.django_db
def test_append_only_guards_still_hold_after_import(livre, tmp_path):
    """bulk_create contourne save() a l'import : il ne doit pas le desarmer."""
    _roundtrip(tmp_path)
    entry = JournalEntry.objects.first()
    entry.description = "modifiee"
    with pytest.raises(RuntimeError):
        entry.save()
    with pytest.raises(RuntimeError):
        entry.delete()
    event = TransactionEvent.objects.first()
    with pytest.raises(RuntimeError):
        event.save()


@pytest.mark.django_db
def test_operator_accounts_can_be_left_out_without_losing_money(livre, tmp_path):
    """Sans le fichier operateurs : les liens d'auteur se vident, l'argent reste.

    Les comptes deja presents ne sont pas touches -- on ne se verrouille
    pas hors de la console pendant une restauration. Mais leurs cles n'ont
    aucune raison de correspondre a celles du fichier : garder les liens
    attribuerait les actions aux mauvaises personnes. On les vide, tous
    etant en SET_NULL.
    """
    before = _roundtrip(tmp_path, operators=False)
    assert _snapshot()["soldes"] == before["soldes"]
    assert User.objects.filter(username="operateur").exists()
    assert JournalEntry.objects.filter(posted_by__isnull=False).count() == 0
    assert AuditLog.objects.filter(user__isnull=False).count() == 0
    assert TransactionEvent.objects.filter(actor__isnull=False).count() == 0


# ----------------------------------------------------------------------
# Les deux niveaux de sensibilite
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_session_tokens_are_never_exported(livre, tmp_path):
    paths = write(tmp_path, passphrase=PASSPHRASE)
    main = paths["principal"].read_text(encoding="utf-8")
    operators = crypto.decrypt(paths["operateurs"], PASSPHRASE)
    assert CustomerToken.objects.exists()
    for content in (main, operators):
        assert "customertoken" not in content
        assert "a" * 64 not in content


@pytest.mark.django_db
def test_the_operator_file_is_encrypted_even_without_the_flag(livre, tmp_path):
    paths = write(tmp_path, encrypt_main=False, passphrase=PASSPHRASE)
    raw = paths["operateurs"].read_bytes()
    assert raw.startswith(b"Salted__")
    assert b"operateur" not in raw
    assert json.loads(crypto.decrypt(paths["operateurs"], PASSPHRASE))["tables"]["accounts.user"]


@pytest.mark.django_db
def test_operator_accounts_are_absent_from_the_main_file(livre, tmp_path):
    """Les deux niveaux de sensibilite : le grand livre peut se partager
    pour analyse, une empreinte de mot de passe jamais."""
    paths = write(tmp_path, passphrase=PASSPHRASE)
    content = paths["principal"].read_text(encoding="utf-8")
    assert "accounts.user" not in json.loads(content)["tables"]
    assert livre["operator"].password not in content
    assert "pbkdf2_" not in content


@pytest.mark.django_db
def test_encrypting_the_main_file_is_reversible(livre, tmp_path):
    clear = write(tmp_path / "clair", passphrase=PASSPHRASE)
    sealed = write(tmp_path / "chiffre", encrypt_main=True, passphrase=PASSPHRASE)
    assert sealed["principal"].name.endswith(".json.enc")
    opened = json.loads(crypto.decrypt(sealed["principal"], PASSPHRASE))
    assert opened["tables"].keys() == json.loads(clear["principal"].read_text(encoding="utf-8"))["tables"].keys()
    with pytest.raises(crypto.DecryptionFailed):
        crypto.decrypt(sealed["principal"], "mauvaise phrase")


# ----------------------------------------------------------------------
# verify_data sur le FICHIER : avant de restaurer, pas apres
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_verify_reads_the_file_without_touching_the_database(livre, tmp_path):
    """La base est VIDEE entre la lecture et la verification : si le mode
    fichier interrogeait la base, il tomberait en marche."""
    payload = build(schema.TABLES)
    restore.restore({"tables": {}}, wipe=True)
    assert not Transaction.objects.exists() and not JournalEntry.objects.exists()
    assert [r.label for r in verify.check_file(payload) if not r.ok] == []


@pytest.mark.django_db
def test_verify_rejects_a_dangling_foreign_key(livre, tmp_path):
    payload = build(schema.TABLES)
    payload["tables"]["ledger.ledgerline"][0]["fields"]["entry"] = 999999
    failed = [r for r in verify.check_file(payload) if not r.ok]
    assert any("ledger.ledgerline" in r.label for r in failed)


@pytest.mark.django_db
def test_verify_rejects_a_truncated_file(livre, tmp_path):
    payload = build(schema.TABLES)
    payload["tables"]["transactions.transaction"] = payload["tables"]["transactions.transaction"][:-1]
    failed = [r for r in verify.check_file(payload) if not r.ok]
    assert any(r.label == "En-tete" for r in failed)


@pytest.mark.django_db
def test_verify_rejects_an_unbalanced_entry_in_the_file(livre, tmp_path):
    payload = build(schema.TABLES)
    line = payload["tables"]["ledger.ledgerline"][0]
    line["fields"]["amount"] = str(Decimal(line["fields"]["amount"]) + Decimal("1"))
    failed = [r for r in verify.check_file(payload) if not r.ok]
    assert {"Equilibre de chaque ecriture", "Somme globale des lignes"} <= {r.label for r in failed}


@pytest.mark.django_db
def test_verify_rejects_an_unknown_format(livre):
    payload = build(schema.TABLES)
    payload["meta"]["format"] = 99
    results = verify.check_file(payload)
    assert results[0].label == "Format du fichier" and not results[0].ok
    assert len(results) == 1, "un format inconnu rend le reste ininterpretable"


# ----------------------------------------------------------------------
# verify_data sur la base
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_verify_passes_on_a_healthy_database(livre):
    assert [r.label for r in verify.check_database() if not r.ok] == []


@pytest.mark.django_db
def test_verify_detects_a_collected_transfer_without_entry(livre):
    orphan = _make(livre["alice"])
    orphan.transition(State.AWAITING_PAYMENT)
    Transaction.objects.filter(pk=orphan.pk).update(payment_confirmed_at=timezone.now())
    failed = {r.label for r in verify.check_database() if not r.ok}
    assert "Transferts encaisses sans ecriture" in failed


@pytest.mark.django_db
def test_verify_detects_an_unbalanced_book(livre):
    line = LedgerLine.objects.first()
    LedgerLine.objects.filter(pk=line.pk).update(amount=line.amount + Decimal("1"))
    failed = {r.label for r in verify.check_database() if not r.ok}
    assert {"Equilibre de chaque ecriture", "Somme globale des lignes"} <= failed


# ----------------------------------------------------------------------
# Les commandes
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_import_refuses_a_non_empty_database(livre, tmp_path, monkeypatch):
    monkeypatch.setenv(crypto.PASSPHRASE_ENV, PASSPHRASE)
    paths = write(tmp_path, passphrase=PASSPHRASE)
    with pytest.raises(CommandError, match="n'est pas vide"):
        call_command("import_data", str(paths["principal"]))
    assert Transaction.objects.count() == 2, "rien ne doit avoir bouge"


@pytest.mark.django_db
def test_the_commands_run_end_to_end(livre, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(crypto.PASSPHRASE_ENV, PASSPHRASE)
    before = _snapshot()
    call_command("export_data", "--output", str(tmp_path), "--encrypt")
    main = next(tmp_path.glob("plipplip-*[!s].json.enc"))
    operators = next(tmp_path.glob("plipplip-*-operateurs.json.enc"))

    call_command("verify_data", str(main))
    call_command("import_data", str(main), "--operateurs", str(operators), "--force")
    call_command("verify_data")
    assert _snapshot() == before


# ----------------------------------------------------------------------
# Garde-fous
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_backup_files_are_ignored_by_git(livre, tmp_path):
    """Les motifs doivent ATTRAPER un nom reellement produit, pas exister."""
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    names = [p.name for p in write(tmp_path, encrypt_main=True, passphrase=PASSPHRASE).values() if hasattr(p, "name")]
    names.append("sauvegardes/plipplip-20260101-020000.json")
    for name in names:
        result = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=root, capture_output=True
        )
        assert result.returncode == 0, f"{name} n'est pas ignore par .gitignore"


def test_every_model_is_either_saved_or_deliberately_excluded():
    """Un modele ajoute au projet ne peut pas rester hors sauvegarde en silence."""
    from django.apps import apps

    known = set(schema.TABLES) | set(schema.OPERATOR_TABLES) | set(schema.EXCLUDED)
    present = {m._meta.label_lower for m in apps.get_models()}
    assert sorted(present - known) == []


def test_every_excluded_model_says_why():
    assert all(reason.strip() for reason in schema.EXCLUDED.values())
