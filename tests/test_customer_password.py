"""Identification par mot de passe, a cote du code SMS.

Le principe : le code prouve qu'on CONTROLE le numero, le mot de passe ne
prouve que l'identite. Donc :
- un Customer ne se cree que pour un numero verifie par code ;
- l'echec par mot de passe dit toujours la meme chose ;
- le verrou par compte est en base et ne bloque que le mot de passe ;
- le verrou par IP est un signal souple, lisible en console.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.accounts import lockout, services
from apps.accounts.models import AuthEvent, AuthMethod, Customer, CustomerAuthEvent, CustomerToken, Role, User
from apps.web.session import AUTH_METHOD_KEY

ROOT = Path(__file__).resolve().parent.parent
CODE = "123456"
PHONE = "50937123456"
OTHER = "50948123456"
UNKNOWN = "50938999999"
GOOD = "maison7"
OTHER_GOOD = "jaden42x"
IP_A = "10.1.1.1"
IP_B = "10.2.2.2"
GENERIC_WEB = "Numéro ou mot de passe incorrect."
GENERIC_API = {"code": "AUTH_FAILED", "message": "Numero ou mot de passe incorrect"}


def _client() -> Client:
    """Navigateur en francais : le site part en creole par defaut, et les
    messages compares ici sont les msgid francais."""
    browser = Client()
    browser.cookies["django_language"] = "fr"
    return browser


@pytest.fixture(autouse=True)
def fast_hashes_and_clean_cache(settings, client):
    client.cookies["django_language"] = "fr"
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    services._dummy_hash.cache_clear()
    cache.clear()
    yield
    cache.clear()
    services._dummy_hash.cache_clear()


def _customer(phone=PHONE, password=GOOD, **fields) -> Customer:
    """Client deja verifie par code, avec un mot de passe."""
    customer = services._customer_for_verified(services.VerifiedPhone(phone))
    if password:
        services.set_password(customer, password)
    if fields:
        Customer.objects.filter(pk=customer.pk).update(**fields)
    customer.refresh_from_db()
    return customer


def _web_password_login(client, phone, password, ip=IP_A):
    return client.post(reverse("web:login_password"), {"phone": phone, "password": password}, REMOTE_ADDR=ip)


def _web_code_login(client, phone=PHONE, ip=IP_A):
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:login"), {"phone": phone}, REMOTE_ADDR=ip)
    return client.post(reverse("web:login_code"), {"code": CODE}, REMOTE_ADDR=ip)


def _api_password_login(phone, password, ip=IP_A):
    return Client().post(
        reverse("api:password_login"), {"phone": phone, "password": password}, content_type="application/json", REMOTE_ADDR=ip
    )


def _api_code_token(phone=PHONE) -> str:
    client = Client()
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("api:otp_request"), {"phone": phone}, content_type="application/json")
    return client.post(reverse("api:otp_verify"), {"phone": phone, "code": CODE}, content_type="application/json").json()["token"]


def _errors(response) -> str:
    return response.content.decode()


# ----------------------------------------------------------------------
# Aucun Customer sans numero verifie
# ----------------------------------------------------------------------
CREATORS = {"create", "get_or_create", "update_or_create", "bulk_create"}


def _creations(tree):
    """(fonction englobante, ligne) de chaque creation de Customer."""
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            current = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.Call):
                func = child.func
                # Customer.objects.<creator>(...)
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr in CREATORS
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "objects"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id == "Customer"
                ):
                    found.append((current, child.lineno))
                # Customer(...)
                if isinstance(func, ast.Name) and func.id == "Customer":
                    found.append((current, child.lineno))
            visit(child, current)

    visit(tree, None)
    return found


def test_architecture_only_the_verified_path_creates_a_customer():
    """Le code source entier : une seule creation de Customer, dans
    _customer_for_verified(), qui n'accepte qu'un VerifiedPhone."""
    creations = []
    for path in (ROOT / "apps").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        for function, line in _creations(ast.parse(path.read_text(encoding="utf-8"))):
            creations.append((path.relative_to(ROOT).as_posix(), function, line))
    assert [(path, function) for path, function, _ in creations] == [
        ("apps/accounts/services.py", "_customer_for_verified")
    ], creations


def test_architecture_verified_phone_is_only_built_after_a_code_check():
    source = (ROOT / "apps/accounts/services.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    builders = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "VerifiedPhone" for c in ast.walk(node))
    ]
    assert builders == ["verify_phone"]
    # Et nulle part ailleurs dans apps/ (les tests ont le droit de le fabriquer).
    for path in (ROOT / "apps").rglob("*.py"):
        if path.name == "services.py" and path.parent.name == "accounts":
            continue
        assert "VerifiedPhone(" not in path.read_text(encoding="utf-8"), path


@pytest.mark.django_db
def test_customer_creation_refuses_anything_but_a_verified_phone():
    with pytest.raises(TypeError):
        services._customer_for_verified(PHONE)
    assert not Customer.objects.exists()


@pytest.mark.django_db
def test_password_login_on_an_unknown_number_creates_nothing(client):
    _web_password_login(client, UNKNOWN, GOOD)
    _api_password_login(UNKNOWN, GOOD)
    with pytest.raises(services.AuthFailed):
        services.authenticate_password(UNKNOWN, GOOD)
    assert not Customer.objects.filter(phone=UNKNOWN).exists()


@pytest.mark.django_db
def test_a_wrong_code_on_reset_creates_nothing():
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        services.request_code(UNKNOWN)
    with pytest.raises(services.InvalidCode):
        services.reset_password(UNKNOWN, "000000", GOOD)
    assert not Customer.objects.filter(phone=UNKNOWN).exists()


# ----------------------------------------------------------------------
# Un seul message d'echec
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_failure_message_is_identical_whatever_the_cause(client):
    _customer(PHONE)
    _customer(OTHER, password="")
    locked = _customer("50946123456", password_locked_until=timezone.now() + timedelta(hours=1))

    cases = {
        "numero inconnu": (UNKNOWN, GOOD),
        "sans mot de passe": (OTHER, GOOD),
        "mot de passe faux": (PHONE, "pas-le-bon1"),
        "compte verrouille (mot de passe JUSTE)": (locked.phone, GOOD),
    }
    web, api = {}, {}
    for label, (phone, password) in cases.items():
        response = _web_password_login(_client(), phone, password)
        assert response.status_code == 400, label
        assert GENERIC_WEB in _errors(response), label
        web[label] = (response.status_code, response.context["password_form"].non_field_errors())
        response = _api_password_login(phone, password)
        api[label] = (response.status_code, response.json()["error"]["code"], response.json()["error"]["message"])

    assert len(set(map(str, web.values()))) == 1, web
    assert set(api.values()) == {(401, GENERIC_API["code"], GENERIC_API["message"])}, api


@pytest.mark.django_db
def test_login_page_offers_both_methods_without_asking_the_number_first(client):
    html = client.get(reverse("web:login")).content.decode()
    assert reverse("web:login") in html and reverse("web:login_password") in html
    assert 'type="password"' in html
    assert reverse("web:password_forgot") in html
    assert "data-reveal" in html  # bouton « Afficher »
    assert "onpaste" not in html  # le collage reste permis


# ----------------------------------------------------------------------
# Verrouillage
# ----------------------------------------------------------------------
def _fail(phone, times, ip=IP_A):
    for _ in range(times):
        with pytest.raises(services.AuthFailed):
            services.authenticate_password(phone, "pas-le-bon1", ip=ip)


@pytest.mark.django_db
def test_account_lock_doubles_from_one_minute_up_to_one_hour(settings):
    customer = _customer()
    delays = []
    for _ in range(12):
        Customer.objects.filter(pk=customer.pk).update(password_locked_until=None)
        customer.refresh_from_db()
        delays.append(lockout.account_failed(customer, ip=IP_A))
    assert delays == [None, None, None, None, 60, 120, 240, 480, 960, 1920, 3600, 3600]


@pytest.mark.django_db
def test_account_lock_is_in_the_database_and_survives_a_cache_flush():
    customer = _customer()
    _fail(PHONE, 5)
    cache.clear()
    with pytest.raises(services.AuthFailed):
        services.authenticate_password(PHONE, GOOD, ip=IP_B)
    customer.refresh_from_db()
    assert customer.password_locked_until > timezone.now()
    assert CustomerAuthEvent.objects.filter(event=AuthEvent.ACCOUNT_LOCKED, customer=customer).count() == 1


@pytest.mark.django_db
def test_an_account_lock_does_not_affect_other_accounts_from_other_ips():
    _customer(PHONE)
    _customer(OTHER, password=OTHER_GOOD)
    _fail(PHONE, 5, ip=IP_A)
    assert services.authenticate_password(OTHER, OTHER_GOOD, ip=IP_B).phone == OTHER


@pytest.mark.django_db
def test_an_ip_block_does_not_affect_the_same_account_from_another_ip(settings):
    settings.PASSWORD_LOCKOUT = {**settings.PASSWORD_LOCKOUT, "IP_THRESHOLD": 3, "ACCOUNT_THRESHOLD": 50}
    _customer(PHONE)
    _fail(UNKNOWN, 3, ip=IP_A)
    assert lockout.ip_blocked(IP_A)
    with pytest.raises(services.AuthFailed):
        services.authenticate_password(PHONE, GOOD, ip=IP_A)
    assert services.authenticate_password(PHONE, GOOD, ip=IP_B).phone == PHONE
    assert CustomerAuthEvent.objects.filter(event=AuthEvent.IP_LOCKED, ip_address=IP_A).count() == 1


@pytest.mark.django_db
def test_a_locked_account_still_logs_in_with_a_code(client):
    _customer()
    _fail(PHONE, 5)
    response = _web_code_login(client)
    assert response.status_code == 302
    assert Customer.objects.get(phone=PHONE).password_failures == 0


@pytest.mark.django_db
def test_a_spoofed_forwarded_header_does_not_escape_the_ip_counter(client, settings):
    """L'IP comptee est celle de apps.accounts.ip, jamais le premier saut
    de X-Forwarded-For, ecrit par le client."""
    settings.PASSWORD_LOCKOUT = {**settings.PASSWORD_LOCKOUT, "IP_THRESHOLD": 3}
    settings.REST_FRAMEWORK = {**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}
    for spoofed in ("1.1.1.1", "2.2.2.2", "3.3.3.3"):
        client.post(
            reverse("web:login_password"),
            {"phone": UNKNOWN, "password": "x1x1x1"},
            HTTP_X_FORWARDED_FOR=f"{spoofed}, {IP_A}",
        )
    assert lockout.ip_blocked(IP_A)
    assert not lockout.ip_blocked("1.1.1.1")


# ----------------------------------------------------------------------
# Les deux methodes
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_a_customer_without_password_still_logs_in_with_a_code(client):
    response = _web_code_login(client)
    assert response.status_code == 302
    customer = Customer.objects.get(phone=PHONE)
    assert not customer.has_password
    assert client.session[AUTH_METHOD_KEY] == AuthMethod.CODE


@pytest.mark.django_db
def test_login_method_is_recorded_on_the_session_and_the_token(client):
    _customer()
    assert _web_password_login(client, PHONE, GOOD).status_code == 302
    assert client.session[AUTH_METHOD_KEY] == AuthMethod.PASSWORD

    token = _api_password_login(PHONE, GOOD).json()["token"]
    assert CustomerToken.objects.get(prefix=token[:12]).auth_method == AuthMethod.PASSWORD
    code_token = _api_code_token()
    assert CustomerToken.objects.get(prefix=code_token[:12]).auth_method == AuthMethod.CODE


@pytest.mark.django_db
def test_both_methods_give_the_same_token_lifetime():
    _customer()
    by_password = _api_password_login(PHONE, GOOD).json()["token"]
    by_code = _api_code_token()
    a, b = (CustomerToken.objects.get(prefix=t[:12]) for t in (by_password, by_code))
    assert abs((a.expires_at - a.created_at) - (b.expires_at - b.created_at)) < timedelta(seconds=5)


@pytest.mark.django_db
def test_a_disabled_account_is_refused_by_both_methods(client):
    customer = _customer()
    # Un code obtenu AVANT la desactivation : il est juste, le compte non.
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        services.request_code(PHONE)
    Customer.objects.filter(pk=customer.pk).update(is_active=False)

    response = _web_password_login(client, PHONE, GOOD)
    assert "Ce compte est désactivé" in _errors(response)
    assert _api_password_login(PHONE, GOOD).json()["error"]["code"] == "ACCOUNT_DISABLED"
    with pytest.raises(services.CustomerDisabled):
        services.authenticate_code(PHONE, CODE)


@pytest.mark.django_db
def test_a_disabled_account_with_a_wrong_password_gets_the_generic_message(client):
    """Le statut du compte ne se revele qu'apres la preuve."""
    _customer(is_active=False)
    assert GENERIC_WEB in _errors(_web_password_login(client, PHONE, "pas-le-bon1"))


# ----------------------------------------------------------------------
# Reinitialisation
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_a_reset_kills_existing_sessions_and_tokens(client):
    _customer()
    other_browser = _client()
    assert _web_password_login(other_browser, PHONE, GOOD).status_code == 302
    assert other_browser.get(reverse("web:transfers")).status_code == 200
    token = _api_password_login(PHONE, GOOD).json()["token"]
    assert Client().get(reverse("api:me"), HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 200

    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("web:password_forgot"), {"phone": PHONE})
    response = client.post(reverse("web:password_reset"), {"code": CODE, "password": OTHER_GOOD})
    assert response.status_code == 302
    assert client.session[AUTH_METHOD_KEY] == AuthMethod.CODE

    assert other_browser.get(reverse("web:transfers")).status_code == 302
    assert Client().get(reverse("api:me"), HTTP_AUTHORIZATION=f"Bearer {token}").status_code == 401
    assert client.get(reverse("web:transfers")).status_code == 200
    assert services.authenticate_password(PHONE, OTHER_GOOD).phone == PHONE
    assert CustomerAuthEvent.objects.filter(event=AuthEvent.PASSWORD_RESET).count() == 1


@pytest.mark.django_db
def test_a_rejected_new_password_does_not_burn_the_code():
    _customer()
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        services.request_code(PHONE)
    with pytest.raises(services.PasswordRejected):
        services.reset_password(PHONE, CODE, "12345678")
    assert services.reset_password(PHONE, CODE, OTHER_GOOD).phone == PHONE


@pytest.mark.django_db
def test_api_reset_revokes_tokens_and_returns_a_code_token():
    _customer()
    old = _api_password_login(PHONE, GOOD).json()["token"]
    client = Client()
    with mock.patch("apps.accounts.otp.generate_code", return_value=CODE):
        client.post(reverse("api:otp_request"), {"phone": PHONE}, content_type="application/json")
    response = client.post(
        reverse("api:password_reset"), {"phone": PHONE, "code": CODE, "password": OTHER_GOOD}, content_type="application/json"
    )
    assert response.status_code == 200
    new = response.json()["token"]
    assert CustomerToken.objects.get(prefix=new[:12]).auth_method == AuthMethod.CODE
    assert Client().get(reverse("api:me"), HTTP_AUTHORIZATION=f"Bearer {old}").status_code == 401


# ----------------------------------------------------------------------
# Creation, changement, regles
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_password_is_offered_after_a_code_login_and_can_be_dismissed(client):
    _web_code_login(client)
    assert 'data-offer="password"' in client.get(reverse("web:transfers")).content.decode()
    client.post(reverse("web:password_offer_dismiss"))
    assert 'data-offer="password"' not in client.get(reverse("web:transfers")).content.decode()


@pytest.mark.django_db
def test_creating_a_password_from_the_site(client):
    _web_code_login(client)
    response = client.post(reverse("web:account_password"), {"password": GOOD})
    assert response.status_code == 302
    customer = Customer.objects.get(phone=PHONE)
    assert customer.has_password and customer.password != GOOD
    assert 'data-offer="password"' not in client.get(reverse("web:transfers")).content.decode()


@pytest.mark.django_db
def test_an_all_digit_password_is_refused_with_an_instruction(client):
    _web_code_login(client)
    response = client.post(reverse("web:account_password"), {"password": "583920"})
    assert response.status_code == 200
    assert "Ajoutez au moins une lettre" in response.content.decode()
    assert not Customer.objects.get(phone=PHONE).has_password


@pytest.mark.parametrize(
    "password, code",
    [("abc12", "too_short"), ("583920", "numeric"), ("azerty", "too_common"), ("a37123456", "similar_phone")],
)
def test_password_rules_are_django_validators_with_a_minimum_of_six(password, code):
    from apps.accounts import passwords

    assert code in passwords.problems(password, PHONE)
    assert passwords.problems("maison7", PHONE) == []
    assert passwords.problems("ab!c7z", PHONE) == []  # 6 suffisent, aucun caractere special exige


@pytest.mark.django_db
def test_changing_the_password_needs_the_current_one_and_keeps_only_this_session(client):
    _customer()
    other_browser = _client()
    _web_password_login(other_browser, PHONE, GOOD)
    _web_password_login(client, PHONE, GOOD)

    response = client.post(reverse("web:account_password"), {"current_password": "faux1x", "password": OTHER_GOOD})
    assert "Mot de passe actuel incorrect" in response.content.decode()

    response = client.post(reverse("web:account_password"), {"current_password": GOOD, "password": OTHER_GOOD})
    assert response.status_code == 302
    assert client.get(reverse("web:transfers")).status_code == 200
    assert other_browser.get(reverse("web:transfers")).status_code == 302


@pytest.mark.django_db
def test_api_set_password_keeps_the_calling_token_and_revokes_the_others():
    customer = _customer(password="")
    first = _api_code_token()
    # Second appareil. Pas de second SMS : le debit d'envoi l'interdit a 1/min.
    second, _ = services._issue_token(customer, AuthMethod.CODE)
    auth = {"HTTP_AUTHORIZATION": f"Bearer {first}"}
    response = Client().post(reverse("api:me_password"), {"password": GOOD}, content_type="application/json", **auth)
    assert response.status_code == 200 and response.json()["has_password"] is True

    response = Client().post(
        reverse("api:me_password"), {"password": OTHER_GOOD, "current_password": GOOD}, content_type="application/json", **auth
    )
    assert response.status_code == 200
    assert Client().get(reverse("api:me"), **auth).status_code == 200
    assert Client().get(reverse("api:me"), HTTP_AUTHORIZATION=f"Bearer {second}").status_code == 401
    customer.refresh_from_db()
    assert customer.auth_epoch == 1


@pytest.mark.django_db
def test_api_rejects_a_weak_password_with_the_rule_codes():
    _customer(password="")
    token = _api_code_token()
    response = Client().post(
        reverse("api:me_password"), {"password": "583920"}, content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}"
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "PASSWORD_INVALID" and "numeric" in error["rules"]


# ----------------------------------------------------------------------
# Journal, console, sauvegardes
# ----------------------------------------------------------------------
@pytest.mark.django_db
def test_failures_are_journaled_without_any_secret():
    customer = _customer()
    _fail(PHONE, 1)
    _fail(UNKNOWN, 1)
    events = CustomerAuthEvent.objects.filter(event=AuthEvent.PASSWORD_FAILED)
    assert {(e.phone, e.customer_id) for e in events} == {(PHONE, customer.pk), (UNKNOWN, None)}
    assert all("pas-le-bon1" not in str(e.detail) for e in CustomerAuthEvent.objects.all())


@pytest.mark.django_db
def test_auth_journal_is_append_only():
    event = CustomerAuthEvent.objects.create(phone=PHONE, event=AuthEvent.PASSWORD_FAILED)
    with pytest.raises(RuntimeError):
        event.save()
    with pytest.raises(RuntimeError):
        event.delete()


@pytest.mark.django_db
def test_ip_blocks_are_visible_in_the_console(client, settings):
    settings.PASSWORD_LOCKOUT = {**settings.PASSWORD_LOCKOUT, "IP_THRESHOLD": 2}
    _fail(UNKNOWN, 2, ip=IP_A)
    client.force_login(User.objects.create_user(username="support", password="x", role=Role.SUPPORT))
    html = client.get(reverse("console:auth_events")).content.decode()
    assert f'data-ip="{IP_A}"' in html


@pytest.mark.django_db
def test_backups_never_carry_customer_password_hashes():
    from apps.backup import export

    _customer()
    rows = export.build(["accounts.customer"])["tables"]["accounts.customer"]
    assert rows and all(row["fields"]["password"] == "" for row in rows)
