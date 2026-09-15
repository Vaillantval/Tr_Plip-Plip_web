import dj_database_url

from .base import *  # noqa: F401,F403

DEBUG = False

SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)  # noqa: F405
PLOPPLOP["CLIENT_ID"] = env("PLOPPLOP_CLIENT_ID", required=True)  # noqa: F405
PLOPPLOP["CLIENT_SECRET"] = env("PLOPPLOP_CLIENT_SECRET", required=True)  # noqa: F405

# Codes SMS : Twilio imperatif en production (jamais le backend console).
# Ses identifiants ne bloquent PAS le demarrage : sans eux, seule
# l'identification des clients est indisponible (check --deploy l'avertit,
# l'envoi de code repond « momentanement impossible »).
OTP["BACKEND"] = "twilio"  # noqa: F405
API_DOCS_ENABLED = env("API_DOCS_ENABLED", "0") == "1"  # noqa: F405

# ----------------------------------------------------------------------
# Base de donnees : DATABASE_URL (Railway : ${{Postgres.DATABASE_URL}}),
# sinon les variables DB_* de base.py.
# ----------------------------------------------------------------------
if env("DATABASE_URL"):  # noqa: F405
    DATABASES["default"] = dj_database_url.parse(  # noqa: F405
        env("DATABASE_URL"),  # noqa: F405
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=env("DATABASE_SSL_REQUIRE", "0") == "1",  # noqa: F405
    )

# ----------------------------------------------------------------------
# Hotes et origines. Railway injecte RAILWAY_PUBLIC_DOMAIN ; son
# healthcheck appelle l'application avec l'hote healthcheck.railway.app.
# ----------------------------------------------------------------------
_railway_domain = env("RAILWAY_PUBLIC_DOMAIN", "")  # noqa: F405
for _host in ("healthcheck.railway.app", _railway_domain):
    if _host and _host not in ALLOWED_HOSTS:  # noqa: F405
        ALLOWED_HOSTS.append(_host)  # noqa: F405

# Origines HTTPS acceptees pour les formulaires : celles fournies, plus
# chaque vrai domaine d'ALLOWED_HOSTS (ni joker, ni IP, ni healthcheck).
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]  # noqa: F405
for _host in ALLOWED_HOSTS:  # noqa: F405
    if _host.startswith(".") or _host == "healthcheck.railway.app" or not any(c.isalpha() for c in _host):
        continue
    if "." in _host and f"https://{_host}" not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(f"https://{_host}")

# Fichiers statiques servis par WhiteNoise, compresses et versionnes
# (collectstatic dans le Dockerfile). En dev, runserver les sert.
MIDDLEWARE.insert(  # noqa: F405
    MIDDLEWARE.index("django.middleware.security.SecurityMiddleware") + 1,  # noqa: F405
    "whitenoise.middleware.WhiteNoiseMiddleware",
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

SECURE_SSL_REDIRECT = True
# Le healthcheck Railway parle HTTP en interne : une redirection HTTPS le
# ferait echouer.
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
