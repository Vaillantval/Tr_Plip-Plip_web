"""Reclamations.

La regle qui domine : une reclamation ne modifie JAMAIS l'etat d'une
transaction, n'ecrit pas au grand livre et ne touche pas au float. Tout le
reste en decoule : cloisonnement par client, journal append-only, audit de
chaque action operateur.
"""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from decimal import Decimal
from unittest import mock

import pytest
from django.urls import reverse

from apps.accounts.models import AuditLog, Customer, Role, User
from apps.claims import services as claims
from apps.claims.models import Author, Claim, ClaimMessage, Reason, Status
from apps.ledger import services as ledger
from apps.ledger.models import JournalEntry, LedgerAccount
from apps.notifications.models import Notification, Template
#: Capture AVANT toute fixture : monkeypatch remplace l'attribut du module,
#: pas l'objet fonction. C'est le vrai _publish, celui qui parle au courtier.
from apps.notifications.services import _publish as REAL_PUBLISH
from apps.transactions import services as transactions
from apps.transactions.models import Transaction, Wallet
from apps.transactions.states import State

SEND = "apps.notifications.sms.send"
HTMX = {"HTTP_HX_REQUEST": "true"}


@pytest.fixture
def books(db):
    ledger.ensure_accounts()


@pytest.fixture(autouse=True)
def _inline_notifications(monkeypatch):
    """Pas de Redis dans les tests : la tache s'execute sur place."""
    from apps.notifications import services as notifications

    def run(notification_id):
        from apps.notifications.tasks import send_notification

        send_notification(notification_id)

    monkeypatch.setattr(notifications, "_publish", run)


@contextmanager
def sending(capture=None, **patch):
    """Patche Twilio AUTOUR de la capture des rappels on_commit.

    L'ordre compte : django_capture_on_commit_callbacks execute les
    rappels en SORTANT de son bloc. Imbrique a l'interieur du patch, il
    s'executerait une fois le patch retire -- et le test partirait
    vraiment chez Twilio.

    Sans `capture`, aucun rappel ne s'execute : pytest-django n'engage
    jamais la transaction du test. C'est ce qu'on veut pour les tests qui
    ne disent rien du SMS ; ceux qui l'observent passent la fixture.
    """
    patch.setdefault("return_value", ("SM-1", "queued"))
    with mock.patch(SEND, **patch) as send:
        if capture is None:
            yield send
        else:
            with capture(execute=True):
                yield send


@pytest.fixture
def alice(books):
    return Customer.objects.create(phone="50937000001")


@pytest.fixture
def bob(books):
    return Customer.objects.create(phone="50937000002")


@pytest.fixture
def operator(db):
    return User.objects.create_user(username="operateur", password="x" * 12, role=Role.OPERATOR)


@pytest.fixture
def support(db):
    return User.objects.create_user(username="soutien", password="x" * 12, role=Role.SUPPORT)


def _transfer(customer) -> Transaction:
    txn = transactions.create_transaction(
        source_wallet=Wallet.MONCASH,
        destination_wallet=Wallet.NATCASH,
        recipient_phone="50932123456",
        net_amount=Decimal("1000"),
        customer=customer,
    )
    txn.transition(State.AWAITING_PAYMENT)
    txn.refresh_from_db()
    return txn


@pytest.fixture
def claim(alice):
    txn = _transfer(alice)
    return claims.open_claim(
        customer=alice, transaction=txn, reason=Reason.NOT_RECEIVED, body="Le beneficiaire n'a rien recu."
    )


def _login(client, customer):
    session = client.session
    session["web:customer_id"] = customer.pk
    session["web:last_seen"] = 9999999999
    session.save()


# ----------------------------------------------------------------------
# La regle qui domine tout
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_a_claim_never_changes_the_transfer_nor_the_books(alice, operator):
    txn = _transfer(alice)
    before_state = txn.state
    before_events = txn.events.count()
    before_entries = JournalEntry.objects.count()

    claim = claims.open_claim(customer=alice, transaction=txn, reason=Reason.OTHER, body="Une question.")
    with sending():
        claims.answer_claim(claim=claim, actor=operator, body="Nous verifions.")
    claims.close_claim(claim=claim, actor=operator, note="Regle.")

    txn.refresh_from_db()
    assert txn.state == before_state
    assert txn.events.count() == before_events
    assert JournalEntry.objects.count() == before_entries
    assert all(a.balance() == Decimal("0.00") for a in LedgerAccount.objects.all())


def test_claim_services_never_touch_transaction_state():
    """Garde d'architecture : aucun service de reclamation n'assigne
    d'etat ni n'appelle transition()."""
    source = inspect.getsource(claims)
    assert "transition(" not in source
    assert ".state =" not in source
    assert "ledger" not in source


def test_services_take_no_request_and_no_http_object():
    """Prets pour DRF : si une vue DRF ne pouvait pas les appeler tels
    quels, le decoupage serait mauvais."""
    for name in ("open_claim", "add_customer_message", "answer_claim", "close_claim"):
        parameters = set(inspect.signature(getattr(claims, name)).parameters)
        assert not parameters & {"request", "session", "view", "response"}, name


def test_client_messages_live_on_the_exceptions_not_in_the_services():
    """Le site et l'API doivent rendre le meme refus a leur facon."""
    for error in (claims.ClaimNotAllowed, claims.ClaimAlreadyOpen, claims.ClaimClosed, claims.InvalidBody):
        assert str(error().message).strip()


# ----------------------------------------------------------------------
# Cloisonnement
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_another_customers_transfer_is_a_404_not_a_403(client, alice, bob):
    txn = _transfer(bob)
    _login(client, alice)
    response = client.get(reverse("web:claim_open", args=[txn.reference]))
    assert response.status_code == 404


@pytest.mark.django_db
def test_the_service_refuses_another_customers_transfer_on_its_own(alice, bob):
    """La garde vit aussi dans le service : une vue DRF n'aura pas
    _own_transfer pour la porter."""
    txn = _transfer(bob)
    with pytest.raises(claims.ClaimNotAllowed):
        claims.open_claim(customer=alice, transaction=txn, reason=Reason.OTHER, body="Pas a moi.")


@pytest.mark.django_db
def test_a_customer_cannot_write_on_another_claim(claim, bob):
    with pytest.raises(claims.ClaimNotAllowed):
        claims.add_customer_message(customer=bob, claim=claim, body="Je m'incruste.")


# ----------------------------------------------------------------------
# Ouverture
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_opening_a_claim_writes_the_first_message(claim):
    assert claim.status == Status.OPEN
    message = claim.messages.get()
    assert message.author_kind == Author.CUSTOMER and message.author is None


@pytest.mark.django_db
def test_only_one_active_claim_per_transfer(claim, alice):
    with pytest.raises(claims.ClaimAlreadyOpen):
        claims.open_claim(
            customer=alice, transaction=claim.transaction, reason=Reason.OTHER, body="Encore une."
        )


@pytest.mark.django_db
def test_a_new_claim_is_possible_once_the_previous_is_closed(claim, alice, operator):
    claims.close_claim(claim=claim, actor=operator)
    again = claims.open_claim(
        customer=alice, transaction=claim.transaction, reason=Reason.OTHER, body="Le probleme revient."
    )
    assert again.pk != claim.pk


@pytest.mark.django_db
@pytest.mark.parametrize("body", ["", "   ", "non"])
def test_an_empty_explanation_is_refused(alice, body):
    txn = _transfer(alice)
    with pytest.raises(claims.InvalidBody):
        claims.open_claim(customer=alice, transaction=txn, reason=Reason.OTHER, body=body)


@pytest.mark.django_db
def test_an_unknown_reason_is_refused(alice):
    txn = _transfer(alice)
    with pytest.raises(claims.InvalidReason):
        claims.open_claim(customer=alice, transaction=txn, reason="parce que", body="Explication valable.")


@pytest.mark.django_db
def test_the_creation_is_throttled(client, alice, monkeypatch):
    from django.core.cache import cache

    cache.clear()
    _login(client, alice)
    refused = 0
    for _ in range(5):
        txn = _transfer(alice)
        response = client.post(
            reverse("web:claim_open", args=[txn.reference]),
            {"reason": Reason.OTHER, "body": "Un probleme de plus."},
        )
        refused += response.status_code == 400
    assert refused, "aucun refus : la limite de debit ne s'applique pas"
    assert Claim.objects.count() <= 3


# ----------------------------------------------------------------------
# Journal append-only
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_the_exchange_journal_cannot_be_rewritten(claim):
    message = claim.messages.get()
    message.body = "Ce n'est pas ce que j'ai dit."
    with pytest.raises(RuntimeError):
        message.save()
    with pytest.raises(RuntimeError):
        message.delete()


@pytest.mark.django_db
def test_closing_keeps_every_message(claim, operator):
    with sending():
        claims.answer_claim(claim=claim, actor=operator, body="Nous avons verifie.")
    claims.close_claim(claim=claim, actor=operator, note="Dossier clos.")
    assert claim.messages.count() == 3


# ----------------------------------------------------------------------
# Cote operateur
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_answering_requires_can_act_and_is_audited(client, claim, support):
    client.force_login(support)
    response = client.post(
        reverse("console:claim_answer", args=[claim.pk]), {"body": "Une reponse."}, **HTMX
    )
    assert response.status_code == 403
    entry = AuditLog.objects.get(action="claim.answer")
    assert entry.allowed is False and entry.target == str(claim.pk)
    claim.refresh_from_db()
    assert claim.status == Status.OPEN


@pytest.mark.django_db
def test_an_operator_can_answer_and_the_action_is_audited(client, claim, operator):
    client.force_login(operator)
    with sending():
        response = client.post(
            reverse("console:claim_answer", args=[claim.pk]), {"body": "Nous relancons l'operateur."}, **HTMX
        )
    assert response.status_code == 200
    claim.refresh_from_db()
    assert claim.status == Status.ANSWERED and claim.answered_at is not None
    assert AuditLog.objects.filter(action="claim.answer", allowed=True).count() == 1
    message = claim.messages.last()
    assert message.author_kind == Author.OPERATOR and message.author == operator


@pytest.mark.django_db
def test_an_empty_answer_leaves_a_second_audit_line(client, claim, operator):
    client.force_login(operator)
    client.post(reverse("console:claim_answer", args=[claim.pk]), {"body": ""}, **HTMX)
    outcomes = [e.detail.get("outcome") for e in AuditLog.objects.filter(action="claim.answer")]
    assert "error" in outcomes
    claim.refresh_from_db()
    assert claim.status == Status.OPEN


@pytest.mark.django_db
def test_a_customer_reply_reopens_the_claim(claim, alice, operator):
    with sending():
        claims.answer_claim(claim=claim, actor=operator, body="Nous verifions.")
    claims.add_customer_message(customer=alice, claim=claim, body="Toujours rien recu.")
    claim.refresh_from_db()
    assert claim.status == Status.OPEN


@pytest.mark.django_db
def test_a_closed_claim_refuses_everything(claim, alice, operator):
    claims.close_claim(claim=claim, actor=operator)
    with pytest.raises(claims.ClaimClosed):
        claims.add_customer_message(customer=alice, claim=claim, body="Je reprends.")
    with pytest.raises(claims.ClaimClosed):
        claims.answer_claim(claim=claim, actor=operator, body="Un mot de plus.")


# ----------------------------------------------------------------------
# Avis SMS
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_the_first_answer_sends_one_notice(claim, operator, django_capture_on_commit_callbacks):
    with sending(django_capture_on_commit_callbacks) as send:
        claims.answer_claim(claim=claim, actor=operator, body="Nous avons relance.")
    assert send.call_count == 1
    note = Notification.objects.get(template=Template.CLAIM_ANSWERED)
    assert note.recipient == claim.customer.phone
    assert note.transaction_id == claim.transaction_id


@pytest.mark.django_db
def test_a_second_answer_sends_nothing_more(claim, operator, django_capture_on_commit_callbacks):
    with sending(django_capture_on_commit_callbacks):
        claims.answer_claim(claim=claim, actor=operator, body="Nous avons relance.")
    with sending(django_capture_on_commit_callbacks) as send:
        claims.answer_claim(claim=claim, actor=operator, body="Toujours en cours.")
    assert send.call_count == 0
    assert Notification.objects.filter(template=Template.CLAIM_ANSWERED).count() == 1


@pytest.mark.django_db
def test_the_notice_never_carries_the_answer(claim, operator):
    secret = "Nous avons rembourse 1000 HTG sur le compte de votre cousin."
    with sending():
        claims.answer_claim(claim=claim, actor=operator, body=secret)
    body = Notification.objects.get(template=Template.CLAIM_ANSWERED).body
    assert secret not in body
    assert claim.transaction.reference in body


@pytest.mark.django_db
def test_closing_without_answering_sends_nothing(claim, operator, django_capture_on_commit_callbacks):
    with sending(django_capture_on_commit_callbacks) as send:
        claims.close_claim(claim=claim, actor=operator, note="Sans suite.")
    assert send.call_count == 0
    assert not Notification.objects.filter(template=Template.CLAIM_ANSWERED).exists()


@pytest.mark.django_db
def test_an_sms_refused_by_twilio_never_blocks_the_answer(claim, operator, django_capture_on_commit_callbacks):
    from apps.providers.twilio.exceptions import TwilioError

    with sending(django_capture_on_commit_callbacks, side_effect=TwilioError("numero invalide", code=21211)):
        claims.answer_claim(claim=claim, actor=operator, body="La reponse passe quand meme.")
    claim.refresh_from_db()
    assert claim.status == Status.ANSWERED
    assert claim.messages.count() == 2
    assert Notification.objects.get(template=Template.CLAIM_ANSWERED).status == "failed"


@pytest.mark.django_db
def test_an_unreachable_queue_never_blocks_the_answer(claim, operator, django_capture_on_commit_callbacks, monkeypatch):
    """Redis injoignable : le rappel on_commit tourne APRES la validation
    en base. Une exception y remonterait jusqu'a l'operateur et lui
    ferait croire que sa reponse a echoue."""
    from apps.notifications import services as notifications

    # La fixture autouse a remplace _publish par un lancement sur place :
    # on remet le vrai, c'est lui que ce test interroge.
    monkeypatch.setattr(notifications, "_publish", REAL_PUBLISH)
    with mock.patch("apps.notifications.tasks.send_notification.apply_async", side_effect=OSError("Redis absent")):
        with django_capture_on_commit_callbacks(execute=True):
            claims.answer_claim(claim=claim, actor=operator, body="La reponse tient.")
    claim.refresh_from_db()
    assert claim.status == Status.ANSWERED
    # La ligne reste a envoyer : le balayage periodique la reprendra.
    assert Notification.objects.get(template=Template.CLAIM_ANSWERED).status == "pending"


# ----------------------------------------------------------------------
# Cote client
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_the_customer_reads_the_answer_on_the_transfer_page(client, claim, operator):
    with sending():
        claims.answer_claim(claim=claim, actor=operator, body="Nous avons relance l'operateur.")
    _login(client, claim.customer)
    html = client.get(reverse("web:transfer_detail", args=[claim.transaction.reference])).content.decode()
    assert "Nous avons relance l&#x27;operateur." in html or "Nous avons relance l'operateur." in html


@pytest.mark.django_db
def test_customer_content_is_escaped_never_marked_safe(client, alice):
    txn = _transfer(alice)
    claims.open_claim(
        customer=alice, transaction=txn, reason=Reason.OTHER, body="<script>alert('vole')</script>"
    )
    _login(client, alice)
    html = client.get(reverse("web:transfer_detail", args=[txn.reference])).content.decode()
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.django_db
def test_the_badge_lights_up_until_the_customer_opens_the_page(client, claim, operator):
    _login(client, claim.customer)
    assert "Réponse à lire" not in client.get(reverse("web:transfers")).content.decode()

    with sending():
        claims.answer_claim(claim=claim, actor=operator, body="Nous avons relance.")
    assert "Réponse à lire" in client.get(reverse("web:transfers")).content.decode()

    client.get(reverse("web:transfer_detail", args=[claim.transaction.reference]))
    assert "Réponse à lire" not in client.get(reverse("web:transfers")).content.decode()


@pytest.mark.django_db
def test_the_console_shows_the_claim_on_the_transfer(client, claim, operator):
    client.force_login(operator)
    html = client.get(
        reverse("console:transaction_detail", args=[claim.transaction.reference])
    ).content.decode()
    assert "signale un probleme" in html
    assert reverse("console:claim_detail", args=[claim.pk]) in html


LOCALE_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent / "apps" / "claims" / "locale"


def _catalog(language):
    """Entrees actives d'un catalogue .po : msgid, msgstr, drapeaux."""
    import ast
    import re

    text = (LOCALE_DIR / language / "LC_MESSAGES" / "django.po").read_text(encoding="utf-8")
    entries = []
    for block in re.split(r"\n\s*\n", text):
        parts, current, flags, obsolete = {"msgid": [], "msgstr": []}, None, [], False
        for line in block.splitlines():
            if line.startswith("#~"):
                obsolete = True
            elif line.startswith("#,"):
                flags += [flag.strip() for flag in line[2:].split(",")]
            elif line.startswith(("msgid ", "msgstr ")):
                current, _, rest = line.partition(" ")
                parts[current].append(ast.literal_eval(rest))
            elif line.startswith('"') and current:
                parts[current].append(ast.literal_eval(line))
        msgid = "".join(parts["msgid"])
        if msgid and not obsolete:
            entries.append({"msgid": msgid, "msgstr": "".join(parts["msgstr"]), "flags": flags})
    return entries


@pytest.mark.parametrize("language", ["en", "ht"])
def test_every_claim_string_is_translated_and_compiled(language):
    """Aucune derogation possible, pas meme une liste d'attente.

    Le pire moment pour lire une langue qu'on ne maitrise pas est celui ou
    l'on essaie de dire « mon argent n'est pas arrive » : le client est
    deja inquiet, et s'il comprend mal la liste des motifs il choisit le
    mauvais -- l'operateur traite alors la mauvaise reclamation.
    """
    po = LOCALE_DIR / language / "LC_MESSAGES" / "django.po"
    entries = _catalog(language)
    # Motifs + etats + les 7 messages d'exception. Les libelles d'Author
    # n'y sont pas : ils ne sortent que dans la console, en francais.
    assert len(entries) == len(Reason.choices) + len(Status.choices) + 7
    assert [e["msgid"] for e in entries if "fuzzy" in e["flags"]] == []
    assert [e["msgid"] for e in entries if not e["msgstr"]] == []

    mo = po.with_suffix(".mo")
    assert mo.exists() and mo.stat().st_mtime >= po.stat().st_mtime - 1


def test_no_claim_string_carries_an_accent_into_an_sms():
    """Les libelles de reclamation ne partent jamais par SMS : l'avis ne
    contient que la reference. Si cela changeait, le codage UCS-2
    doublerait le cout de chaque envoi."""
    from apps.notifications import messages as sms_messages

    body = sms_messages.claim_answered(mock.Mock(reference="PP-0000-XXXXXXXX"))
    assert body.isascii()
    assert len(body) <= 160, "un avis qui depasse 160 caracteres coute deux segments"


@pytest.mark.django_db
def test_no_template_marks_claim_content_as_safe():
    """Aucun |safe ni mark_safe dans tout le projet : on gele cet etat."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "apps"
    offenders = []
    for path in list(root.rglob("*.html")) + list(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "|safe" in text or "mark_safe" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
