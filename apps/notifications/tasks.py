"""Envoi asynchrone des notifications.

File Celery par defaut, jamais 'payouts' : un appel HTTP a Twilio sur le
worker des retraits bloquerait la file des decaissements, qui n'a qu'un
seul processus.
"""

from __future__ import annotations

import logging
import random
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.providers.twilio import exceptions as tw

logger = logging.getLogger(__name__)

#: Reprise bornee : 30 s, 60 s, 120 s, 240 s, plus une gigue pour ne pas
#: relancer tout un lot a la meme seconde.
BACKOFF_SECONDS = 30


def _backoff(retries: int) -> int:
    return int(BACKOFF_SECONDS * (2**retries) + random.uniform(0, 10))


@shared_task(name="notifications.send_notification", bind=True, acks_late=True, max_retries=4)
def send_notification(self, notification_id: int) -> str:
    """Envoie un SMS. Toujours vers un statut terminal, jamais en suspens."""
    from . import services

    note = services.claim(notification_id)
    if note is None:
        return "skipped"  # deja envoye, trop vieux, ou trop de tentatives

    try:
        return services.deliver(note)
    except (tw.TwilioRateLimited, tw.TwilioUnavailable) as exc:
        if self.request.retries >= self.max_retries:
            # Pas d'autoretry_for : il laisserait la ligne « a envoyer »
            # pour toujours une fois les reprises epuisees.
            services._finish(note, status=services.Status.FAILED, error_code="UNAVAILABLE", error=str(exc))
            return "failed"
        raise self.retry(exc=exc, countdown=_backoff(self.request.retries))


@shared_task(name="notifications.sweep_pending")
def sweep_pending(limit: int = 100) -> dict:
    """Reprend les notifications qu'aucune tache ne suit plus.

    on_commit publie la tache APRES le commit : un processus tue entre les
    deux laisse une ligne « a envoyer » que personne ne reprendrait.
    """
    from .models import Notification, Status

    now = timezone.now()
    stuck = Notification.objects.filter(
        status=Status.PENDING,
        # Laisser sa chance a la tache publiee par on_commit.
        created_at__lte=now - timedelta(minutes=5),
        created_at__gte=now - timedelta(seconds=settings.NOTIFICATIONS["MAX_AGE_SECONDS"]),
        attempts__lt=settings.NOTIFICATIONS["MAX_ATTEMPTS"],
    ).order_by("created_at")[:limit]

    published = 0
    for note in stuck:
        send_notification.apply_async((note.pk,), expires=settings.NOTIFICATIONS["MAX_AGE_SECONDS"])
        published += 1
    return {"republished": published}
