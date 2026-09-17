from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from django.conf.locale import LANG_INFO

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
    "drf_spectacular",
    "apps.accounts",
    "apps.ledger",
    "apps.transactions",
    "apps.treasury",
    "apps.console",
    "apps.api",
    "apps.web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.web.middleware.CustomerSessionMiddleware",
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

# Site client : francais par defaut, creole et anglais. La console reste
# en francais. Django ne connait pas le creole haitien : il est declare
# ici, ses traductions vivent dans apps/web/locale/ht.
LANGUAGE_CODE = "fr"
LANGUAGES = [
    ("fr", "Français"),
    ("ht", "Kreyòl ayisyen"),
    ("en", "English"),
]
LANG_INFO.setdefault("ht", {"bidi": False, "code": "ht", "name": "Haitian Creole", "name_local": "Kreyòl ayisyen"})
TIME_ZONE = "America/Port-au-Prince"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "console:login"
LOGIN_REDIRECT_URL = "console:dashboard"
LOGOUT_REDIRECT_URL = "console:login"

# Session client du site web : deconnexion apres 30 min d'inactivite.
WEB_SESSION_IDLE_SECONDS = int(env("WEB_SESSION_IDLE_SECONDS", "1800"))

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

# Duree de vie du verrou des decaissements, RAFRAICHI a chaque etape du lot.
# Doit couvrir l'etape la plus longue sans rafraichissement : un retrait
# complet, soit 3 appels HTTP (connexion + lecture, TIMEOUT chacune).
# C'est aussi le delai maximal pendant lequel un worker tue bloque la file.
PAYOUT_LOCK_TTL_SECONDS = int(
    env("PAYOUT_LOCK_TTL_SECONDS", str(int(6 * PLOPPLOP["TIMEOUT"] + 60)))
)

# Cooldown impose par plopplop entre deux retraits, par IP.
# A revoir des que le plafond aura ete negocie : c'est ce chiffre qui
# determine le debit maximal de la plateforme.
PAYOUT_COOLDOWN_SECONDS = int(env("PAYOUT_COOLDOWN_SECONDS", "125"))
PAYOUT_MAX_ATTEMPTS = int(env("PAYOUT_MAX_ATTEMPTS", "3"))

# Intervalle de beat de la tache de decaissement ; une tache restee en
# file plus longtemps est abandonnee (expires).
PAYOUT_DRAIN_INTERVAL_SECONDS = int(env("PAYOUT_DRAIN_INTERVAL_SECONDS", "30"))

# Worker de decaissement a l'arret : file non vide et aucun retrait tente
# depuis ce delai. Alerte console, et plus de delai affiche aux clients.
PAYOUT_STALL_SECONDS = int(env("PAYOUT_STALL_SECONDS", str(3 * PAYOUT_COOLDOWN_SECONDS)))

# Un decaissement « en cours » au-dela de ce delai n'a plus de processus
# derriere lui : le worker a ete tue pendant l'appel a plopplop, et la
# fenetre s'ouvre a chaque deploiement. Seuil large : un retrait legitime
# dure quelques secondes, et un faux positif coute une verification.
PAYOUT_INFLIGHT_STALE_SECONDS = int(env("PAYOUT_INFLIGHT_STALE_SECONDS", "600"))

# Au-dela, aucun delai n'est annonce au client (message sans duree).
ETA_MAX_DISPLAY_SECONDS = int(env("ETA_MAX_DISPLAY_SECONDS", "3600"))

# api/paiement-verify ne renvoie jamais "echoue" : l'expiration est une
# decision locale. 30 minutes par defaut.
PAYMENT_EXPIRY_SECONDS = int(env("PAYMENT_EXPIRY_SECONDS", "1800"))

# Un PAYOUT_PENDING de dix minutes est normal. Au-dela de ce seuil,
# l'argent est probablement parti sans confirmation : la console le
# remonte en tete des exceptions, il faut appeler l'operateur. 2 h.
PAYOUT_PENDING_STALE_SECONDS = int(env("PAYOUT_PENDING_STALE_SECONDS", "7200"))

# Un 404 juste apres un timeout peut etre une course ecriture/lecture chez
# plopplop. Un PAYOUT_UNKNOWN ne repart en file qu'apres deux 404 pour la
# meme reference, espaces d'au moins ce delai. 10 min.
PAYOUT_VERIFY_GRACE_SECONDS = int(env("PAYOUT_VERIFY_GRACE_SECONDS", "600"))

# ----------------------------------------------------------------------
# Tarification
# ----------------------------------------------------------------------
# Les taux (frais client, couts plopplop, commission) se reglent dans la
# console par le superadmin : ecran « Methodes et tarifs ».
PRICING = {
    "MAX_NET_AMOUNT": Decimal(env("MAX_NET_AMOUNT", "50000")),
}

# Plafonds cumules par client : les MONTANTS se reglent dans la console
# (superadmin), la LONGUEUR des fenetres est ici -- elle definit le sens
# des deux plafonds, ce n'est pas un bouton metier.
TRANSFER_LIMITS = {
    "day": int(env("LIMIT_DAY_SECONDS", str(24 * 3600))),
    "month": int(env("LIMIT_MONTH_SECONDS", str(30 * 24 * 3600))),
}

TREASURY = {
    "THRESHOLDS": {
        "warning": Decimal(env("FLOAT_WARNING", "50000")),
        "critical": Decimal(env("FLOAT_CRITICAL", "20000")),
    }
}

# ----------------------------------------------------------------------
# API publique
# ----------------------------------------------------------------------
REST_FRAMEWORK = {
    # Jeton client uniquement : la session de la console n'ouvre pas l'API.
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.api.authentication.CustomerTokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["apps.api.permissions.IsCustomer"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.api.errors.handler",
    "DEFAULT_THROTTLE_RATES": {
        "otp_request_phone_burst": "1/min",
        "otp_request_phone": "5/hour",
        "otp_verify_phone": "10/hour",
        "otp_ip": "30/hour",
        "public_ip": "120/min",
        "transfer_create": "20/hour",
        "customer_read": "120/min",
    },
    # Nombre de proxies de confiance devant l'application. A 0, l'adresse
    # IP retenue est REMOTE_ADDR. Laisser DRF lire X-Forwarded-For sans
    # borne permettrait de contourner toutes les limites par IP.
    "NUM_PROXIES": int(env("API_NUM_PROXIES", "0")),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "API Plip-Plip",
    "DESCRIPTION": "Transferts MonCash ↔ NatCash. Montants en HTG, en chaines decimales.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {"WalletEnum": "apps.transactions.models.Wallet"},
}
API_DOCS_ENABLED = env("API_DOCS_ENABLED", "1") == "1"

API_TOKEN_TTL_SECONDS = int(env("API_TOKEN_TTL_SECONDS", str(30 * 24 * 3600)))

OTP = {
    # "console" en developpement uniquement : le code est ecrit dans les logs.
    "BACKEND": env("OTP_BACKEND", "console"),
    "CODE_TTL_SECONDS": int(env("OTP_CODE_TTL_SECONDS", "600")),
    "MAX_CHECK_ATTEMPTS": 5,
}

TWILIO = {
    "ACCOUNT_SID": env("TWILIO_ACCOUNT_SID", ""),
    "AUTH_TOKEN": env("TWILIO_AUTH_TOKEN", ""),
    "VERIFY_SERVICE_SID": env("TWILIO_VERIFY_SERVICE_SID", ""),
    "TIMEOUT": float(env("TWILIO_TIMEOUT", "10")),
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
    "drain-payouts": {
        "task": "transactions.drain_payout_queue",
        "schedule": float(PAYOUT_DRAIN_INTERVAL_SECONDS),
        # Un lot peut durer ~10 min : sans expiration, les declenchements
        # s'empilent dans la file 'payouts'.
        "options": {"expires": PAYOUT_DRAIN_INTERVAL_SECONDS},
    },
    "sweep-stale-payouts": {"task": "transactions.sweep_stale_payouts", "schedule": 120.0},
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
