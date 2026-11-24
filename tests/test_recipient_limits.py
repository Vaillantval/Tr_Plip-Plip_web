"""Plafond du BENEFICIAIRE, par numero.

Ce plafond n'a pas la meme nature que celui du client. Le plafond
expediteur est une limite que nous PROMETTONS ; celui-ci est une borne de
SURVEILLANCE : beaucoup d'expediteurs vers un meme numero est le schema
que la lutte anti-blanchiment cherche a detecter.

Ce que ces tests protegent, par ordre d'importance :

  - le numero ne peut pas fuir l'activite d'un tiers a l'expediteur, ni
    directement (aucun chiffre dans la reponse) ni PAR DIFFERENCE (sonder
    des montants decroissants jusqu'a ce que ca passe) ;
  - deux formats du meme numero consomment le meme plafond ;
  - la trace du refus, qui vaut plus que le refus lui-meme.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest import mock

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import AuditLog, Customer, Role
from apps.ledger import services as ledger
from apps.transactions import limits, services
from apps.transactions.models import Transaction, TransferLimitPolicy, Wallet
from apps.transactions.states import State


@pytest.fixture
def client(client):
    """Ces tests lisent les textes francais du site, dont le creole est la
    langue par defaut : le client choisit le francais, comme avec le
    selecteur de langue."""
    client.cookies["django_language"] = "fr"
    return client


RECIPIENT = "50932123456"
#: Le meme numero, tel qu'un client le saisirait : sans indicatif, avec des
#: separateurs. C'est par la que le plafond se contournerait.
RECIPIENT_LOCAL = "32 12-34.56"
CAP_DAY = Decimal("150000")
CAP_MONTH = Decimal("600000")
GET_CLIENT = "apps.transactions.services.get_client"


@pytest.fixture(autouse=True)
def books(db):
    ledger.ensure_accounts()
    TransferLimitPolicy.objects.update_or_create(
        pk=1,
        defaults={
            "daily_cap": Decimal("50000"),
            "monthly_cap": Decimal("200000"),
            "recipient_daily_cap": CAP_DAY,
            "recipient_monthly_cap": CAP_MONTH,
        },
    )
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def senders(db):
    """Plusieurs expediteurs : c'est le schema que ce plafond vise."""
    return [Customer.objects.create(phone=f"5093700000{n}") for n in range(1, 6)]


def _create(customer, amount, *, recipient=RECIPIENT):
    return services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone=recipient,
        net_amount=Decimal(amount),
        customer=customer,
        **({} if customer else {}),
    )


def _collected(customer, amount, *, recipient=RECIPIENT, ago_seconds=0):
    txn = _create(customer, amount, recipient=recipient)
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)
    if ago_seconds:
        Transaction.objects.filter(pk=txn.pk).update(
            payment_confirmed_at=timezone.now() - timedelta(seconds=ago_seconds)
        )
    return Transaction.objects.get(pk=txn.pk)


def _fill(senders, total, *, recipient=RECIPIENT, ago_seconds=0):
    """Encaisse `total` vers le numero, reparti sur plusieurs expediteurs.

    Chacun reste sous SON plafond : seul celui du beneficiaire peut
    arreter ce schema.
    """
    remaining = Decimal(total)
    for customer in senders:
        if remaining <= 0:
            break
        slice_ = min(remaining, Decimal("50000"))
        _collected(customer, slice_, recipient=recipient, ago_seconds=ago_seconds)
        remaining -= slice_
    assert remaining <= 0, "pas assez d'expediteurs pour atteindre ce total"


def _reserved(customer, amount, *, recipient=RECIPIENT, minutes=30):
    """Paiement demande, pas encore obtenu. Une transaction simplement
    CREATED ne reserve rien : _conditions() exige une echeance a venir."""
    txn = _create(customer, amount, recipient=recipient)
    txn.transition(State.AWAITING_PAYMENT)
    Transaction.objects.filter(pk=txn.pk).update(payment_expires_at=timezone.now() + timedelta(minutes=minutes))
    return Transaction.objects.get(pk=txn.pk)


def _used(recipient=RECIPIENT, window=limits.DAY):
    return {w.name: w.used for w in limits.recipient_consumption(recipient)}[window]


# ----------------------------------------------------------------------
# Piege 1 : la normalisation
# ----------------------------------------------------------------------
def test_two_formats_of_the_same_number_share_one_cap(senders):
    """Sans normalisation a l'ecriture, le plafond se contourne en
    changeant le format de saisie."""
    _collected(senders[0], "50000", recipient=RECIPIENT)
    _collected(senders[1], "50000", recipient=RECIPIENT_LOCAL)

    assert _used() == Decimal("100000.00")
    assert Transaction.objects.filter(recipient_phone=RECIPIENT).count() == 2
    assert Transaction.objects.exclude(recipient_phone=RECIPIENT).count() == 0


def test_creation_normalises_the_recipient_even_when_the_caller_does_not(senders):
    """La garantie est au point d'ecriture, pas chez l'appelant : ni le
    formulaire du site ni le serialiseur ne sont dans ce chemin."""
    txn = _create(senders[0], "1000", recipient="+509 32-12.34 56")

    assert txn.recipient_phone == RECIPIENT


def test_creation_normalises_the_sender_number_too(senders):
    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone=RECIPIENT,
        sender_phone="37 12-34.56",
        net_amount=Decimal("1000"),
        customer=senders[0],
    )

    assert txn.sender_phone == "50937123456"


def test_an_unusable_number_is_refused_at_creation(senders):
    """Un numero qui ne peut pas recevoir n'a rien a faire en base.

    Sans ce controle, le transfert serait cree, encaisse, puis echouerait
    au DECAISSEMENT : l'argent du client entre nos mains, et un operateur
    a mobiliser pour le rendre. Le controle de validite appartient au
    moment ou l'on ne doit encore rien a personne.
    """
    from apps.accounts.phone import NotAMobileNumber

    before = Transaction.objects.count()
    with pytest.raises(NotAMobileNumber):
        _create(senders[0], "1000", recipient="50912345678")  # fixe, pas mobile

    assert Transaction.objects.count() == before, "rien ne doit avoir ete ecrit"


def test_a_landline_is_told_it_cannot_receive_money_not_that_it_is_malformed(client, senders):
    """Le message decide de ce que le client fait ensuite.

    « Numero invalide » devant un fixe parfaitement ecrit le pousse a le
    ressaisir a l'identique, puis a recommencer. Il doit apprendre que ce
    numero-la ne peut pas recevoir d'argent.
    """
    session = client.session
    session["web:customer_id"] = senders[0].pk
    session["web:last_seen"] = 9999999999
    session.save()

    html = client.post(
        reverse("web:send"),
        {
            "source_wallet": Wallet.MONCASH,
            "destination_wallet": Wallet.NATCASH,
            "recipient_phone": "50912345678",  # fixe, bien ecrit
            "net_amount": "1000",
            "sender_phone": "",
        },
    ).content.decode()

    assert "ne peut pas recevoir d" in html
    assert "lignes mobiles" in html
    assert "invalide" not in html, "un fixe bien ecrit n'est pas une faute de frappe"


# ----------------------------------------------------------------------
# Piege 2 : l'index
# ----------------------------------------------------------------------
def test_the_recipient_window_index_exists():
    """Le controle balaie 30 jours par numero a chaque creation."""
    names = {tuple(index.fields) for index in Transaction._meta.indexes}

    assert ("recipient_phone", "payment_confirmed_at") in names


# ----------------------------------------------------------------------
# La borne
# ----------------------------------------------------------------------
def test_a_transfer_exactly_at_the_cap_is_accepted(senders):
    _fill(senders, "140000")

    txn = _create(senders[4], "10000")

    assert txn.net_amount == Decimal("10000")


def test_one_gourde_over_the_cap_is_refused(senders):
    _fill(senders, "140000")
    before = Transaction.objects.count()

    with pytest.raises(services.RecipientLimitExceeded) as raised:
        _create(senders[4], "10000.01")

    assert raised.value.window.name == limits.DAY
    assert raised.value.phone == RECIPIENT
    assert Transaction.objects.count() == before  # rien n'a ete ecrit


def test_the_monthly_cap_blocks_while_the_day_is_free(senders):
    """Le refus doit rester lisible : journee libre, mois plein."""
    # On tourne sur les expediteurs : chacun doit rester sous SON plafond
    # mensuel (200 000), seul celui du beneficiaire peut arreter ce schema.
    for day in range(1, 13):
        _collected(senders[day % len(senders)], "50000", ago_seconds=day * 24 * 3600)

    assert _used(window=limits.DAY) == Decimal("0.00")
    with pytest.raises(services.RecipientLimitExceeded) as raised:
        _create(senders[0], "10000")

    assert raised.value.window.name == limits.MONTH


def test_reservations_count_toward_the_recipient_cap(senders):
    """Sinon le contournement revient : creer sans payer, puis tout payer."""
    for customer in senders[:3]:
        _reserved(customer, "50000")

    assert _used() == Decimal("150000.00")
    with pytest.raises(services.RecipientLimitExceeded):
        _create(senders[3], "1000")


def test_an_expired_reservation_frees_the_cap(senders):
    for customer in senders[:3]:
        txn = _reserved(customer, "50000")
        Transaction.objects.filter(pk=txn.pk).update(payment_expires_at=timezone.now() - timedelta(seconds=1))

    assert _used() == Decimal("0.00")
    assert _create(senders[3], "1000").net_amount == Decimal("1000")


def test_a_refund_frees_the_recipient_cap(senders, django_user_model):
    operator = django_user_model.objects.create_user(username="op", password="x" * 12, role=Role.OPERATOR)
    _fill(senders[:3], "150000")
    refunded = Transaction.objects.filter(recipient_phone=RECIPIENT).first()

    services.refund(refunded, reason="erreur", transfer_reference="MAN-1", actor=operator)

    assert _used() == Decimal("100000.00")
    assert _create(senders[3], "10000").net_amount == Decimal("10000")


def test_an_encaissed_transfer_is_never_blocked_afterwards(senders):
    """Verifie a la creation, jamais apres : une transaction encaissee est
    une dette, aucun plafond ne bloque son decaissement."""
    _fill(senders, "140000")
    txn = _collected(senders[4], "10000")

    TransferLimitPolicy.objects.filter(pk=1).update(recipient_daily_cap=Decimal("50000"))
    with mock.patch(GET_CLIENT) as get_client:
        get_client.return_value.withdraw.return_value = mock.Mock(
            succeeded=True, fee=Decimal("40"), transaction_id="PP-1", api_reference="", balance_after=None
        )
        services.execute_payout(txn)

    assert Transaction.objects.get(pk=txn.pk).state == State.COMPLETED


# ----------------------------------------------------------------------
# Console : le controle ne s'applique pas, le compteur si
# ----------------------------------------------------------------------
def test_a_console_transfer_is_never_refused_but_still_counts(senders):
    """Un operateur doit pouvoir reemettre vers un numero sature, typiquement
    pour reparer notre propre erreur. Mais le capteur doit le voir."""
    _fill(senders, "150000")

    txn = services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone=RECIPIENT,
        net_amount=Decimal("20000"),
        customer=None,
    )
    txn.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(txn, provider_amount=txn.total_charged)

    assert txn.pk is not None
    assert _used() == Decimal("170000.00"), "un transfert de console doit compter dans la consommation"


def test_a_console_transfer_does_not_count_as_a_distinct_sender(senders):
    _fill(senders[:2], "100000")
    console = services.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone=RECIPIENT,
        net_amount=Decimal("1000"),
        customer=None,
    )
    console.transition(State.AWAITING_PAYMENT)
    services.confirm_payment(console, provider_amount=console.total_charged)

    day = limits.recipient_consumption(RECIPIENT)[0]
    assert limits.distinct_senders(RECIPIENT, day) == 2


# ----------------------------------------------------------------------
# La trace, qui vaut plus que le refus
# ----------------------------------------------------------------------
def _refuse_through_api(senders, amount="20000"):
    """Passe par create_transfer : c'est lui qui ecrit la trace, hors de la
    transaction annulee."""
    from apps.api import services as api_services

    q = services.quote_transfer(
        source_wallet=Wallet.MONCASH, destination_wallet=Wallet.NATCASH, net_amount=Decimal(amount)
    )
    with pytest.raises(services.RecipientLimitExceeded):
        api_services.create_transfer(
            customer=senders[4],
            idempotency_key=f"cle-{amount}",
            source_wallet=Wallet.MONCASH,
            destination_wallet=Wallet.NATCASH,
            recipient_phone=RECIPIENT,
            net_amount=Decimal(amount),
            expected_total=q.total_charged,
        )


def test_the_refusal_is_audited_with_the_number_and_the_sender_count(senders):
    _fill(senders[:4], "150000")

    _refuse_through_api(senders)

    log = AuditLog.objects.get(action="limit.recipient_refused")
    assert log.allowed is False
    assert log.target == RECIPIENT
    assert log.detail["window"] == limits.DAY
    assert log.detail["requested"] == "20000.00"
    assert log.detail["senders"] == 3, "le nombre d'expediteurs distincts est LE signal"


def test_every_refusal_is_audited_with_its_requested_amount(senders):
    """Une suite de montants decroissants vers un meme numero est un signal
    de sondage : il doit se lire dans le journal."""
    _fill(senders[:4], "150000")

    for amount in ("30000", "20000", "12000"):
        _refuse_through_api(senders, amount)

    amounts = sorted(log.detail["requested"] for log in AuditLog.objects.filter(action="limit.recipient_refused"))
    assert amounts == ["12000.00", "20000.00", "30000.00"]


# ----------------------------------------------------------------------
# La fuite — directe, puis par difference
# ----------------------------------------------------------------------
def test_no_amount_and_no_date_leak_to_the_sender(client, senders):
    """Fuite DIRECTE : ni la consommation, ni le plafond, ni une date."""
    _fill(senders[:4], "150000")
    session = client.session
    session["web:customer_id"] = senders[4].pk
    session["web:last_seen"] = 9999999999
    session["web:draft"] = {
        "source_wallet": Wallet.MONCASH,
        "destination_wallet": Wallet.NATCASH,
        "recipient_phone": RECIPIENT,
        "net_amount": "20000",
        "sender_phone": "",
    }
    session.save()

    page = client.get(reverse("web:confirm")).content.decode()
    key = page.split('name="idempotency_key" value="')[1].split('"')[0]
    total = page.split('name="expected_total" value="')[1].split('"')[0]
    html = client.post(
        reverse("web:confirm"), {"idempotency_key": key, "expected_total": total, "recipient_phone_confirm": RECIPIENT}
    ).content.decode()

    assert "ne peut pas recevoir ce montant" in html
    # Ni le plafond, ni ce que le numero a deja recu, ni ce qu'il lui reste.
    for leak in ("150 000", "150000", "600 000", "600000", "10 000,00", "frees_at"):
        assert leak not in html, f"fuite : {leak}"
    # Et surtout aucun conseil de verifier le numero : il est bon.
    assert "rifiez le num" not in html


def test_the_api_refusal_carries_no_detail_at_all(senders):
    """Aucun `extra` : ni plafond, ni consommation, ni date."""
    from apps.api import views

    exc = services.RecipientLimitExceeded(
        "interne : 150000 + 20000 > 150000",
        phone=RECIPIENT,
        amount=Decimal("20000"),
        window=limits.recipient_consumption(RECIPIENT)[0],
        senders=4,
    )

    error = views._recipient_limit_error(exc)
    rendered = str(error.detail)

    assert error.error_code == "RECIPIENT_LIMIT_EXCEEDED"
    assert error.status_code == 422
    assert error.extra == {}
    assert rendered == views.RECIPIENT_LIMIT_MESSAGE
    assert "150000" not in rendered and RECIPIENT not in rendered
    assert "rifiez le num" not in rendered


def test_probing_with_decreasing_amounts_stops_answering(senders):
    """Fuite PAR DIFFERENCE, celle que le message constant ne couvre pas.

    Refuse a 30 000, accepte a 12 000 : l'expediteur connait la marge du
    beneficiaire. Passe trois refus, la frontiere cesse de repondre.
    """
    _fill(senders[:4], "140000")  # il reste 10 000
    prober = senders[4]

    for amount in ("30000", "20000", "12000"):
        with pytest.raises(services.RecipientLimitExceeded):
            _create(prober, amount)

    # 5 000 passerait : il reste 10 000. La reponse ne doit plus le dire.
    with pytest.raises(services.RecipientLimitExceeded) as raised:
        _create(prober, "5000")

    assert raised.value.throttled is True
    assert raised.value.window is None, "un refus etrangle n'a pas consulte le plafond"


def test_the_probe_throttle_is_scoped_to_the_sender(senders):
    _fill(senders[:3], "140000")
    prober, innocent = senders[3], senders[4]

    for amount in ("30000", "20000", "12000"):
        with pytest.raises(services.RecipientLimitExceeded):
            _create(prober, amount)

    # Un autre expediteur n'est pas puni pour le sondage du premier.
    assert _create(innocent, "5000").net_amount == Decimal("5000")


def test_a_throttled_refusal_is_audited_too(senders):
    _fill(senders[:4], "150000")

    for amount in ("30000", "20000", "12000", "5000"):
        _refuse_through_api(senders, amount)

    logs = AuditLog.objects.filter(action="limit.recipient_refused")
    assert logs.count() == 4
    assert logs.filter(detail__throttled=True).count() == 1


# ----------------------------------------------------------------------
# Concurrence : depassement borne, et visible
# ----------------------------------------------------------------------
def test_two_concurrent_creations_overshoot_by_one_transfer_at_most(senders):
    """Aucun verrou, volontairement : ce plafond est une borne de
    surveillance, pas une limite promise. On gele la borne du depassement.
    """
    _fill(senders[:3], "140000")  # il reste 10 000

    # Deux creations qui ont chacune lu la consommation avant l'autre.
    consumption = limits.recipient_consumption(RECIPIENT)
    with mock.patch.object(limits, "recipient_consumption", return_value=consumption):
        first = _create(senders[3], "10000")
        second = _create(senders[4], "10000")
    for txn in (first, second):
        txn.transition(State.AWAITING_PAYMENT)
        services.confirm_payment(txn, provider_amount=txn.total_charged)

    assert _used() == Decimal("160000.00"), "depassement d'un transfert, pas davantage"

    # La suivante est refusee normalement -- par le plafond du beneficiaire,
    # donc par un expediteur qui a encore de la marge sur le sien.
    with pytest.raises(services.RecipientLimitExceeded):
        _create(senders[2], "1000")


def test_the_console_shows_the_recipient_consumption_and_flags_an_overshoot(client, senders, django_user_model):
    operator = django_user_model.objects.create_user(username="op2", password="x" * 12, role=Role.OPERATOR)
    _fill(senders, "150000")
    txn = Transaction.objects.filter(recipient_phone=RECIPIENT).first()
    TransferLimitPolicy.objects.filter(pk=1).update(recipient_daily_cap=Decimal("100000"))
    client.force_login(operator)

    html = client.get(reverse("console:transaction_detail", args=[txn.reference])).content.decode()

    assert "Plafonds du bénéficiaire" in html
    assert RECIPIENT in html
    assert "DÉPASSÉ" in html, "un depassement invisible est un capteur en panne"
    assert "expéditeur" in html


# ----------------------------------------------------------------------
# Reglage
# ----------------------------------------------------------------------
def _login(client, django_user_model, role=Role.SUPERADMIN):
    user = django_user_model.objects.create_user(username=f"u-{role}", password="x" * 12, role=role)
    client.force_login(user)
    return user


FULL = {
    "daily_cap": "50000",
    "monthly_cap": "200000",
    "recipient_daily_cap": "150000",
    "recipient_monthly_cap": "600000",
}


@pytest.mark.parametrize(
    "caps, expected",
    [
        ({"recipient_daily_cap": "40000"}, "journalier du beneficiaire"),
        ({"recipient_monthly_cap": "150000"}, "mensuel du beneficiaire"),
    ],
)
def test_recipient_caps_below_sender_caps_are_refused_on_both_windows(
    client, django_user_model, caps, expected
):
    """Un plafond de reception plus bas que celui d'emission refuserait un
    transfert que l'expediteur a le droit de faire."""
    _login(client, django_user_model)

    response = client.post(reverse("console:limits_update"), {**FULL, **caps}, HTTP_HX_REQUEST="true")

    assert expected in response.content.decode()
    assert "NON enregistres" in response.content.decode()
    assert TransferLimitPolicy.objects.get(pk=1).recipient_daily_cap == CAP_DAY


def test_the_superadmin_changes_the_recipient_caps_and_it_is_audited(client, django_user_model):
    user = _login(client, django_user_model)

    client.post(
        reverse("console:limits_update"),
        {**FULL, "recipient_daily_cap": "200000", "recipient_monthly_cap": "800000"},
        HTTP_HX_REQUEST="true",
    )

    policy = TransferLimitPolicy.objects.get(pk=1)
    assert (policy.recipient_daily_cap, policy.recipient_monthly_cap, policy.updated_by) == (
        Decimal("200000"), Decimal("800000"), user,
    )
    log = AuditLog.objects.get(action="limits.update")
    assert log.allowed is True and log.detail["recipient_daily_cap"] == "200000"


@pytest.mark.parametrize("role", [Role.OPERATOR, Role.APPROVER, Role.SUPPORT, Role.AUDITOR])
def test_only_the_superadmin_changes_the_recipient_caps(client, django_user_model, role):
    _login(client, django_user_model, role)

    response = client.post(
        reverse("console:limits_update"),
        {**FULL, "recipient_daily_cap": "999999"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 403
    assert TransferLimitPolicy.objects.get(pk=1).recipient_daily_cap == CAP_DAY


def test_a_ratio_under_two_warns_without_blocking(client, django_user_model):
    """Avertissement, pas erreur : un tel reglage refusera des expediteurs
    innocents pour l'activite d'un tiers. Le superadmin doit le voir venir."""
    _login(client, django_user_model)

    response = client.post(
        reverse("console:limits_update"),
        {**FULL, "recipient_daily_cap": "60000", "recipient_monthly_cap": "600000"},
        HTTP_HX_REQUEST="true",
    )
    html = response.content.decode()

    assert TransferLimitPolicy.objects.get(pk=1).recipient_daily_cap == Decimal("60000"), "enregistre quand meme"
    assert "Réglage à regarder de près" in html
    assert "1,2" in html, "le rapport doit etre affiche, pas seulement l'alerte"
