"""Superadmin cree au deploiement depuis les variables d'environnement.

Regles protegees : les variables font foi (creation, puis alignement du
mot de passe) ; un mot de passe absent ou faible ne cree rien et ne fait
pas echouer le deploiement ; rien n'est ecrit sans changement ; chaque
changement est audite sans le mot de passe ; le pre-deploiement l'execute.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command

from apps.accounts.models import AuditLog, Role, User

STRONG = "Plip-Console-2026!kx"
OTHER_STRONG = "Nouvelle-Phrase-Secrete-77"


def _run(monkeypatch, **env):
    for key in ("SUPERADMIN_USERNAME", "SUPERADMIN_EMAIL", "SUPERADMIN_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    out, err = StringIO(), StringIO()
    call_command("init_superadmin", stdout=out, stderr=err)
    return out.getvalue() + err.getvalue()


@pytest.mark.django_db
def test_creates_the_superadmin_from_environment(monkeypatch):
    output = _run(monkeypatch, SUPERADMIN_PASSWORD=STRONG)

    user = User.objects.get(username="info@plip.ht")
    assert (user.email, user.role, user.is_superuser, user.is_staff, user.is_active) == (
        "info@plip.ht", Role.SUPERADMIN, True, True, True,
    )
    assert user.check_password(STRONG)
    assert "cree" in output and STRONG not in output
    log = AuditLog.objects.get(action="superadmin.init")
    assert log.detail == {"changes": ["created"]} and STRONG not in str(log.detail)


@pytest.mark.django_db
def test_username_and_email_are_configurable(monkeypatch):
    _run(monkeypatch, SUPERADMIN_USERNAME="val", SUPERADMIN_EMAIL="val@plip.ht", SUPERADMIN_PASSWORD=STRONG)
    user = User.objects.get(username="val")
    assert user.email == "val@plip.ht" and user.check_password(STRONG)


@pytest.mark.django_db
def test_redeploy_without_change_writes_nothing(monkeypatch):
    _run(monkeypatch, SUPERADMIN_PASSWORD=STRONG)
    password_hash = User.objects.get(username="info@plip.ht").password

    output = _run(monkeypatch, SUPERADMIN_PASSWORD=STRONG)

    assert "deja a jour" in output
    assert User.objects.get(username="info@plip.ht").password == password_hash
    assert AuditLog.objects.filter(action="superadmin.init").count() == 1


@pytest.mark.django_db
def test_changing_the_variable_changes_the_password(monkeypatch):
    _run(monkeypatch, SUPERADMIN_PASSWORD=STRONG)

    _run(monkeypatch, SUPERADMIN_PASSWORD=OTHER_STRONG)

    user = User.objects.get(username="info@plip.ht")
    assert user.check_password(OTHER_STRONG) and not user.check_password(STRONG)
    changes = [log.detail["changes"] for log in AuditLog.objects.filter(action="superadmin.init").order_by("id")]
    assert changes == [["created"], ["password"]]


@pytest.mark.django_db
def test_existing_account_is_restored_as_active_superadmin(monkeypatch):
    User.objects.create_user(username="info@plip.ht", password=STRONG, role=Role.SUPPORT, is_active=False)

    _run(monkeypatch, SUPERADMIN_PASSWORD=STRONG)

    user = User.objects.get(username="info@plip.ht")
    assert (user.role, user.is_superuser, user.is_staff, user.is_active) == (Role.SUPERADMIN, True, True, True)
    changes = AuditLog.objects.get(action="superadmin.init").detail["changes"]
    assert "password" not in changes and {"role", "is_superuser", "is_active"} <= set(changes)


@pytest.mark.django_db
def test_empty_password_creates_nothing_and_does_not_fail(monkeypatch):
    output = _run(monkeypatch, SUPERADMIN_PASSWORD="")
    assert "vide" in output
    assert not User.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("weak", ["court1!", "12345678901234", "password1234", "info@plip.ht"])
def test_weak_password_is_refused_without_failing_the_deploy(monkeypatch, weak):
    output = _run(monkeypatch, SUPERADMIN_PASSWORD=weak)
    assert "refuse" in output and weak not in output.split("(")[0]
    assert not User.objects.exists()


@pytest.mark.django_db
def test_weak_new_password_leaves_the_existing_one_in_place(monkeypatch):
    _run(monkeypatch, SUPERADMIN_PASSWORD=STRONG)
    _run(monkeypatch, SUPERADMIN_PASSWORD="123456789012")
    assert User.objects.get(username="info@plip.ht").check_password(STRONG)


def test_predeploy_runs_init_superadmin_last():
    with mock.patch("apps.console.management.commands.predeploy.call_command") as call:
        call_command("predeploy", stdout=StringIO())
    assert [c.args[0] for c in call.call_args_list] == ["check", "migrate", "init_ledger", "init_superadmin"]
