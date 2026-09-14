from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def env(key: str, default=None, *, required: bool = False) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        raise RuntimeError(f"Variable d'environnement manquante : {key}")
    return value


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = False
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "").split(",") if h]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.accounts",
    "apps.ledger",
    "apps.transactions",
    "apps.treasury",
    "apps.console",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
AUTH_USER_MODEL = "accounts.User"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "plipplip"),
        "USER": env("DB_USER", "plipplip"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5432"),
        "ATOMIC_REQUESTS": False,
    }
}

LANGUAGE_CODE = "fr-ht"
TIME_ZONE = "America/Port-au-Prince"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "console:login"
LOGIN_REDIRECT_URL = "console:dashboard"

# ----------------------------------------------------------------------
# plopplop
# ----------------------------------------------------------------------
# Le client_secret ne doit JAMAIS etre expose au navigateur ni a l'app
# mobile. Il ne sort pas de ce processus.
PLOPPLOP = {
    "BASE_URL": env("PLOPPLOP_BASE_URL", "https://plopplop.solutionip.app"),
    "CLIENT_ID": env("PLOPPLOP_CLIENT_ID", ""),
    "CLIENT_SECRET": env("PLOPPLOP_CLIENT_SECRET", ""),
    "TIMEOUT": float(env("PLOPPLOP_TIMEOUT", "30")),
}

# Cooldown impose par plopplop entre deux retraits, par IP.
# A revoir des que le plafond aura ete negocie : c'est ce chiffre qui
# determine le debit maximal de la plateforme.
PAYOUT_COOLDOWN_SECONDS = int(env("PAYOUT_COOLDOWN_SECONDS", "125"))
PAYOUT_MAX_ATTEMPTS = int(env("PAYOUT_MAX_ATTEMPTS", "3"))

# api/paiement-verify ne renvoie jamais "echoue" : l'expiration est une
# decision locale. 30 minutes par defaut.
PAYMENT_EXPIRY_SECONDS = int(env("PAYMENT_EXPIRY_SECONDS", "1800"))

# ----------------------------------------------------------------------
# Tarification
# ----------------------------------------------------------------------
# Taux issus de la note conceptuelle, PAS d'une mesure. A recalibrer des
# que plopplop aura communique ce qu'il retient sur un encaissement.
PRICING = {
    "RATES": {
        "in": Decimal(env("RATE_IN", "0.03")),
        "out": Decimal(env("RATE_OUT", "0.03")),
        "platform": Decimal(env("RATE_PLATFORM", "0.03")),
    },
    "MAX_NET_AMOUNT": Decimal(env("MAX_NET_AMOUNT", "50000")),
}

TREASURY = {
    "THRESHOLDS": {
        "warning": Decimal(env("FLOAT_WARNING", "50000")),
        "critical": Decimal(env("FLOAT_CRITICAL", "20000")),
    }
}

# ----------------------------------------------------------------------
# Celery / cache
# ----------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", "redis://localhost:6379/0")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_ROUTES = {
    # File dediee : un seul worker, concurrence 1. Voir le README.
    "transactions.drain_payout_queue": {"queue": "payouts"},
}
CELERY_BEAT_SCHEDULE = {
    "poll-payments": {"task": "transactions.poll_pending_payments", "schedule": 20.0},
    "drain-payouts": {"task": "transactions.drain_payout_queue", "schedule": 30.0},
    "resolve-unknown": {"task": "transactions.resolve_unknown_payouts", "schedule": 300.0},
    "check-float": {"task": "transactions.check_float_level", "schedule": 600.0},
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"std": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "std"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "apps.transactions": {"level": "INFO"},
        "apps.providers.plopplop": {"level": "INFO"},
    },
}
