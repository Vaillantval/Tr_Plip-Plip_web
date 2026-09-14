"""Configuration de deploiement Railway.

Les reglages de prod sont charges dans un sous-processus : ils exigent
des secrets et modifient des listes globales, ils ne doivent pas polluer
la configuration des autres tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from django.urls import reverse

ROOT = Path(__file__).resolve().parent.parent

PROD_ENV = {
    "DJANGO_SETTINGS_MODULE": "config.settings.prod",
    "DJANGO_SECRET_KEY": "x" * 60,
    "PLOPPLOP_CLIENT_ID": "id",
    "PLOPPLOP_CLIENT_SECRET": "secret",
    "TWILIO_ACCOUNT_SID": "AC1",
    "TWILIO_AUTH_TOKEN": "token",
    "TWILIO_VERIFY_SERVICE_SID": "VA1",
    "DATABASE_URL": "postgresql://plip:motdepasse@postgres.railway.internal:5432/railway",
    "REDIS_URL": "redis://default:pw@redis.railway.internal:6379",
    "RAILWAY_PUBLIC_DOMAIN": "plip-plip-web.up.railway.app",
    "DJANGO_ALLOWED_HOSTS": "plipplip.ht,www.plipplip.ht",
    "CSRF_TRUSTED_ORIGINS": "https://admin.plipplip.ht",
}

PROBE = """
import json, django
django.setup()
from django.conf import settings
db = settings.DATABASES["default"]
print(json.dumps({
    "engine": db["ENGINE"], "host": db["HOST"], "name": db["NAME"], "conn_max_age": db["CONN_MAX_AGE"],
    "allowed_hosts": settings.ALLOWED_HOSTS, "csrf": settings.CSRF_TRUSTED_ORIGINS,
    "broker": settings.CELERY_BROKER_URL, "otp": settings.OTP["BACKEND"], "debug": settings.DEBUG,
    "redirect_exempt": settings.SECURE_REDIRECT_EXEMPT,
    "middleware": settings.MIDDLEWARE[:3],
}))
"""


def _prod_settings(**overrides) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "DATABASE_", "RAILWAY_"))}
    env.update(PROD_ENV)
    env.update(overrides)
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_prod_settings_read_railway_database_redis_and_domains():
    conf = _prod_settings()

    assert (conf["engine"], conf["host"], conf["name"]) == ("django.db.backends.postgresql", "postgres.railway.internal", "railway")
    assert conf["conn_max_age"] == 600
    assert conf["broker"] == PROD_ENV["REDIS_URL"]
    assert conf["otp"] == "twilio" and conf["debug"] is False
    for host in ("plipplip.ht", "www.plipplip.ht", "plip-plip-web.up.railway.app", "healthcheck.railway.app"):
        assert host in conf["allowed_hosts"], host
    assert set(conf["csrf"]) >= {
        "https://admin.plipplip.ht", "https://plipplip.ht", "https://plip-plip-web.up.railway.app",
    }
    assert "https://healthcheck.railway.app" not in conf["csrf"]
    assert conf["redirect_exempt"] == [r"^health/$"]
    assert conf["middleware"][1] == "whitenoise.middleware.WhiteNoiseMiddleware"


def test_prod_settings_refuse_to_start_without_secrets():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "TWILIO_", "PLOPPLOP_"))}
    env["DJANGO_SETTINGS_MODULE"] = "config.settings.prod"
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY" in result.stderr


@pytest.mark.django_db
def test_health_endpoint_checks_the_database(client):
    response = client.get(reverse("health"))
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def _toml(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text(encoding="utf-8"))


def test_railway_services_share_the_dockerfile_and_split_the_queues():
    web = _toml("railway.toml")
    payouts = _toml("railway-celery-payouts.toml")
    general = _toml("railway-celery.toml")
    beat = _toml("railway-beat.toml")

    for conf in (web, payouts, general, beat):
        assert conf["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile"}
    assert web["deploy"]["preDeployCommand"] == ["python manage.py predeploy"]
    assert web["deploy"]["healthcheckPath"] == "/health/"
    assert "$PORT" in web["deploy"]["startCommand"]

    # Retraits : file dediee, un seul a la fois.
    assert "-Q payouts" in payouts["deploy"]["startCommand"]
    assert "--concurrency=1" in payouts["deploy"]["startCommand"]
    # Le worker general ne doit jamais consommer la file des retraits.
    assert "-Q celery" in general["deploy"]["startCommand"] and "payouts" not in general["deploy"]["startCommand"]
    assert "beat" in beat["deploy"]["startCommand"]
    for conf in (payouts, general, beat):
        assert "healthcheckPath" not in conf["deploy"]


def test_payout_task_is_routed_to_the_dedicated_queue(settings):
    assert settings.CELERY_TASK_ROUTES["transactions.drain_payout_queue"] == {"queue": "payouts"}


def test_dockerignore_keeps_secrets_and_local_state_out_of_the_image():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for entry in (".env", ".venv", ".git", "dev.sqlite3"):
        assert entry in ignored, entry
