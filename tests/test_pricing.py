"""Tarification reglable par le superadmin et couts plopplop.

Regles protegees :
  - les devis utilisent les tarifs enregistres, par portefeuille ;
  - une transaction garde son devis et ses couts figes ;
  - les encaissements creditent le float, net du cout d'encaissement ;
  - seul le superadmin modifie la tarification, et c'est audite ;
  - le tableau de bord signale au superadmin une tarification non validee
    et, a tous, une route ouverte a marge negative.
"""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

import pytest
from django.urls import reverse

from apps.accounts.models import AuditLog, Role, User
from apps.ledger import services as ledger
from apps.ledger.models import JournalEntry, LedgerAccount
from apps.providers.plopplop import exceptions as pp
from apps.providers.plopplop.client import WithdrawalStatus
from apps.transactions import services
from apps.transactions.models import Wallet
from apps.transactions.states import State
from apps.treasury import services as treasury

GET_CLIENT = "apps.transactions.services.get_client"
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def books(db):
    ledger.ensure_accounts()


def _user(role) -> User:
    return User.objects.create_user(username=f"u-{role}", password="x", role=role)


def _current_rates() -> tuple[Decimal, dict]:
    overview = services.pricing_overview()
    rates = {w["wallet"]: {f: w[f] for f in services.RATE_FIELDS} for w in overview["wallets"]}
    return overview["policy"].platform_fee_rate, rates


def _set_rates(**changes):
    """changes : wallet=dict(champ=taux) ; platform=taux."""
    platform, rates = _current_rates()
    platform = changes.pop("platform", platform)
    for wallet, fields in changes.items():
        rates[wallet].update(fields)
    services.update_pricing(platform_fee_rate=platform, wallet_rates=rates, actor=None)


def _txn(source=Wallet.MONCASH, destination=Wallet.NATCASH, net=Decimal("1000")):
    return services.create_transaction(
        source_wallet=source, destination_wallet=destination, recipient_phone="50932123456", net_amount=net
    )


def _post_form(client, overrides=None):
    data = {"platform_fee_rate": "3"}
    for wallet in services.wallet_availability():
        for field in services.RATE_FIELDS:
            payout_only = field.startswith("payout")
            if payout_only and not wallet["payout_capable"]:
                continue
            data[f"{wallet['wallet']}__{field}"] = str((wallet[field] * 100).normalize())
    data.update(overrides or {})
    return client.post(reverse("console:pricing_update"), data, **HTMX)


# ----------------------------------------------------------------------
# Devis et couts
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_starting_rates_reproduce_the_concept_note_and_plopplop_withdrawal_fees():
    to_natcash = _txn(Wallet.MONCASH, Wallet.NATCASH)
    to_moncash = _txn(Wallet.NATCASH, Wallet.MONCASH)

    assert to_natcash.total_charged == to_moncash.total_charged == Decimal("1090.00")
    assert to_natcash.provider_fee_out_estimate == Decimal("25.00")  # NatCash 2,5 %
    assert to_moncash.provider_fee_out_estimate == Decimal("40.00")  # MonCash 4 %
    assert to_natcash.margin_estimate == Decimal("65.00")
    assert to_moncash.margin_estimate == Decimal("50.00")


@pytest.mark.django_db
def test_quotes_use_the_rates_of_source_and_destination_wallets():
    _set_rates(carte={"payment_fee_rate": Decimal("0.05")}, moncash={"payout_fee_rate": Decimal("0.04")}, platform=Decimal("0.02"))

    q = services.quote_transfer(source_wallet="carte", destination_wallet="moncash", net_amount=Decimal("1000"))

    assert (q.fee_in, q.fee_out, q.fee_platform, q.total_charged) == (
        Decimal("50.00"), Decimal("40.00"), Decimal("20.00"), Decimal("1110.00"),
    )


@pytest.mark.django_db
def test_existing_transactions_keep_their_frozen_quote_and_costs():
    txn = _txn()

    _set_rates(natcash={"payout_fee_rate": Decimal("0.10"), "payout_cost_rate": Decimal("0.09")})

    txn.refresh_from_db()
    assert (txn.total_charged, txn.provider_fee_out_estimate) == (Decimal("1090.00"), Decimal("25.00"))
    assert _txn().total_charged == Decimal("1160.00")


@pytest.mark.django_db
def test_payment_credits_the_float_net_of_the_estimated_collection_cost(books):
    _set_rates(moncash={"payment_cost_rate": Decimal("0.01")})
    txn = _txn()
    txn.transition(State.AWAITING_PAYMENT)

    services.confirm_payment(txn, provider_amount=txn.total_charged)

    def balance(code):
        return LedgerAccount.objects.get(code=code).balance()

    assert balance(ledger.FLOAT) == Decimal("1079.10")  # 1090 - 1 %
    assert balance(ledger.EXPENSE_FEES) == Decimal("10.90")
    assert balance(ledger.CASH_SETTLEMENT) == Decimal("0")
    assert all(e.is_balanced() for e in JournalEntry.objects.all())


@pytest.mark.django_db
def test_payout_settled_by_verification_uses_the_frozen_withdrawal_cost(books):
    txn = _txn(Wallet.NATCASH, Wallet.MONCASH)
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    txn.refresh_from_db()
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.side_effect = pp.PlopPlopIndeterminate("timeout")
        services.execute_payout(txn)
    txn.refresh_from_db()

    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdrawal_status.return_value = WithdrawalStatus(
            reference=txn.payout_reference, status="success", transaction_id="WD-1", amount=None
        )
        services.verify_unknown_payout(txn)

    txn.refresh_from_db()
    assert (txn.state, txn.payout_fee_actual) == (State.COMPLETED, Decimal("40.00"))  # MonCash 4 %, pas 6 %


@pytest.mark.django_db
def test_runway_follows_net_float_flow_not_gross_outflows(books):
    ledger.record_float_topup(Decimal("5000"), reference="RCH-1")
    txn = _txn()
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)

    assert treasury.float_net_flow(24) == Decimal("1090.00")  # le rechargement n'est pas de l'activite
    assert treasury.runway_hours() is None


# ----------------------------------------------------------------------
# Reglage superadmin
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_superadmin_saves_pricing_in_percent_and_it_counts_as_review(client):
    admin = _user(Role.SUPERADMIN)
    client.force_login(admin)
    assert services.pricing_overview()["unreviewed"]

    response = _post_form(client, {"moncash__payout_fee_rate": "4", "natcash__payout_fee_rate": "2.5"})

    assert response.status_code == 200 and b'id="methods-body"' in response.content
    overview = services.pricing_overview()
    wallets = {w["wallet"]: w for w in overview["wallets"]}
    assert (wallets["moncash"]["payout_fee_rate"], wallets["natcash"]["payout_fee_rate"]) == (
        Decimal("0.0400"), Decimal("0.0250"),
    )
    assert not overview["unreviewed"] and overview["policy"].reviewed_by == admin
    log = AuditLog.objects.get(action="pricing.update")
    assert log.allowed and log.detail["moncash__payout_fee_rate"] == "4"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "overrides",
    [{"platform_fee_rate": "-1"}, {"natcash__payout_cost_rate": "50"}, {"moncash__payment_fee_rate": "abc"}],
)
def test_invalid_pricing_is_refused_and_audited(client, overrides):
    client.force_login(_user(Role.SUPERADMIN))
    before = _current_rates()

    response = _post_form(client, overrides)

    assert "NON enregistree" in response.content.decode()
    assert _current_rates() == before
    assert services.pricing_overview()["unreviewed"]
    assert AuditLog.objects.filter(action="pricing.update", detail__outcome="error").count() == 1


def test_payout_rates_cannot_be_set_on_wallets_plopplop_cannot_pay_to(db):
    platform, rates = _current_rates()
    rates["kashpaw"]["payout_cost_rate"] = Decimal("0.01")
    with pytest.raises(services.InvalidPricing):
        services.update_pricing(platform_fee_rate=platform, wallet_rates=rates, actor=None)


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.OPERATOR, Role.APPROVER, Role.SUPPORT, Role.AUDITOR])
def test_only_superadmin_can_change_pricing(client, role):
    client.force_login(_user(role))
    before = _current_rates()

    page = client.get(reverse("console:methods"))
    response = _post_form(client, {"platform_fee_rate": "10"})

    assert b'data-console-action="pricing"' not in page.content
    assert response.status_code == 403
    assert _current_rates() == before
    assert AuditLog.objects.get(action="pricing.update").allowed is False


# ----------------------------------------------------------------------
# Tableau de bord
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_superadmin_is_told_on_the_dashboard_until_pricing_is_validated(client, books):
    admin = _user(Role.SUPERADMIN)
    client.force_login(admin)

    first = client.get(reverse("console:dashboard")).content
    assert b'data-pricing-banner="unreviewed"' in first
    assert b"data-pricing-card" in first

    _post_form(client)

    after = client.get(reverse("console:dashboard")).content
    assert b'data-pricing-banner="unreviewed"' not in after
    assert b"data-pricing-card" in after


@pytest.mark.django_db
@pytest.mark.parametrize("role", [Role.OPERATOR, Role.SUPPORT])
def test_other_roles_do_not_get_the_validation_banner(client, books, role):
    client.force_login(_user(role))
    content = client.get(reverse("console:dashboard")).content
    assert b'data-pricing-banner="unreviewed"' not in content
    assert b"data-pricing-card" not in content


@pytest.mark.django_db
def test_open_route_with_negative_margin_is_flagged_to_everyone(client, books):
    _set_rates(moncash={"payout_cost_rate": Decimal("0.12")})  # cout > frais factures
    client.force_login(_user(Role.OPERATOR))

    content = client.get(reverse("console:dashboard")).content.decode()

    assert 'data-pricing-banner="loss"' in content
    assert "NatCash → MonCash" in content
    loss = services.pricing_overview()["loss_routes"]
    assert {(r["source"]["wallet"], r["destination"]["wallet"]) for r in loss} == {
        ("natcash", "moncash"), ("kashpaw", "moncash"), ("carte", "moncash"),
    }

    services.set_wallet_availability("moncash", direction="payout", enabled=False)
    assert services.pricing_overview()["loss_routes"] == []  # une route fermee n'alerte pas
