"""Twilio ne bloque pas le deploiement.

Sans identifiants Twilio, la plateforme demarre et passe le
pre-deploiement ; seule l'identification SMS des clients est
indisponible, sans aucun appel a Twilio. plopplop reste obligatoire.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest
import responses
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

ROOT = Path(__file__).resolve().parent.parent
PHONE = "50937123456"

PROD_WITHOUT_TWILIO = {
    "DJANGO_SETTINGS_MODULE": "config.settings.prod",
    "DJANGO_SECRET_KEY": "k3y-" + "x9" * 30,
    "PLOPPLOP_CLIENT_ID": "id",
    "PLOPPLOP_CLIENT_SECRET": "secret",
    "DJANGO_ALLOWED_HOSTS": "plip.ht",
}


def _env(**overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "TWILIO_", "PLOPPLOP_", "DATABASE_"))}
    env.update(PROD_WITHOUT_TWILIO)
    env.update(overrides)
    return env


def test_production_deploy_checks_pass_without_twilio():
    result = subprocess.run(
        [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "ERROR"],
        cwd=ROOT, env=_env(), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "plipplip.W002" in result.stdout + result.stderr


def test_production_still_refuses_to_start_without_plopplop():
    result = subprocess.run(
        [sys.executable, "manage.py", "check"],
        cwd=ROOT, env=_env(PLOPPLOP_CLIENT_SECRET=""), capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "PLOPPLOP_CLIENT_SECRET" in result.stderr


@pytest.fixture
def twilio_missing(settings):
    cache.clear()
    settings.OTP = {**settings.OTP, "BACKEND": "twilio"}
    settings.TWILIO = {"ACCOUNT_SID": "", "AUTH_TOKEN": "", "VERIFY_SERVICE_SID": "", "TIMEOUT": 5}
    # Aucune requete HTTP autorisee : un appel a Twilio ferait echouer le test.
    with responses.RequestsMock(assert_all_requests_are_fired=False):
        yield
    cache.clear()


@pytest.mark.django_db
def test_api_login_reports_sms_unavailable_without_calling_twilio(twilio_missing):
    api = APIClient()
    request = api.post(reverse("api:otp_request"), {"phone": PHONE}, format="json")
    verify = api.post(reverse("api:otp_verify"), {"phone": PHONE, "code": "123456"}, format="json")

    assert (request.status_code, request.json()["error"]["code"]) == (503, "OTP_UNAVAILABLE")
    assert (verify.status_code, verify.json()["error"]["code"]) == (503, "OTP_UNAVAILABLE")


@pytest.mark.django_db
def test_web_login_reports_sms_unavailable_and_the_site_stays_up(client, twilio_missing):
    assert client.get(reverse("web:home")).status_code == 200
    response = client.post(reverse("web:login"), {"phone": PHONE})
    assert response.status_code == 200
    assert "momentanément impossible" in response.content.decode()
