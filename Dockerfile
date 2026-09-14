# Image unique pour les 4 services Railway : web, celery-payouts,
# celery-worker, celery-beat. Seule la commande de demarrage change
# (railway*.toml).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

WORKDIR /app

# psycopg[binary] embarque libpq : aucune dependance systeme a compiler.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Fichiers statiques figes dans l'image (WhiteNoise). Les reglages de prod
# exigent des secrets : on passe des valeurs factices a CETTE commande
# seulement, elles ne sont pas conservees dans l'image.
RUN DJANGO_SECRET_KEY=collectstatic-only \
    PLOPPLOP_CLIENT_ID=build PLOPPLOP_CLIENT_SECRET=build \
    TWILIO_ACCOUNT_SID=build TWILIO_AUTH_TOKEN=build TWILIO_VERIFY_SERVICE_SID=build \
    python manage.py collectstatic --noinput

# Ne pas tourner en root.
RUN adduser --system --group --no-create-home app && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["sh", "-c", "gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000} --workers 3 --threads 4 --worker-class gthread --timeout 60 --access-logfile -"]
