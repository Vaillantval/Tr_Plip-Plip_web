from .base import *  # noqa: F401,F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

DATABASES["default"] = {  # noqa: F405
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": BASE_DIR / "dev.sqlite3",  # noqa: F405
}

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Le cooldown reste applique en dev : le comportement de la file doit
# etre le meme qu'en production, sinon on developpe contre une fiction.
CELERY_TASK_ALWAYS_EAGER = False
