"""Statuts de livraison renvoyes par Twilio (StatusCallback).

Seul usage : apprendre qu'un WhatsApp n'a pas pu etre livre, pour envoyer
l'avis par SMS. Les autres statuts sont acquittes et ignores.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.providers.twilio import signature

from . import services

logger = logging.getLogger(__name__)


@csrf_exempt  # appel serveur a serveur : la signature Twilio remplace le jeton CSRF
@require_POST
def twilio_status(request):
    conf = settings.TWILIO
    # L'URL signee est celle que NOUS avons donnee a Twilio, pas celle que
    # Django reconstruit : derriere le proxy de Railway, schema et hote
    # peuvent differer, et la signature ne correspondrait jamais.
    query = request.META.get("QUERY_STRING", "")
    url = f"{conf['STATUS_CALLBACK_URL']}?{query}" if query else conf["STATUS_CALLBACK_URL"]
    params = {key: request.POST.get(key, "") for key in request.POST}
    if not signature.is_valid(url, params, request.headers.get("X-Twilio-Signature", ""), conf["AUTH_TOKEN"]):
        logger.warning("Statut Twilio refuse : signature invalide")
        return HttpResponseForbidden()

    try:
        notification_id = int(request.GET.get("n", ""))
    except ValueError:
        return HttpResponse(status=204)
    services.record_whatsapp_status(
        notification_id=notification_id,
        message_sid=params.get("MessageSid", ""),
        message_status=params.get("MessageStatus", ""),
        error_code=params.get("ErrorCode", ""),
    )
    return HttpResponse(status=204)
