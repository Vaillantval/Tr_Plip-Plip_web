"""Notifications SMS au client qui a envoye l'argent.

Deux evenements seulement : transfert livre, transfert rembourse. Rien au
beneficiaire -- l'operateur le notifie deja, et nous n'avons pas son
consentement.

Un echec de notification ne doit JAMAIS faire echouer un transfert : ni
l'ecriture de la ligne, ni l'envoi, ni un Twilio absent.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction as db_transaction
from django.utils import timezone, translation

from apps.providers.twilio import exceptions as tw

from . import messages, sms
from .models import Channel, Notification, Status, Template

logger = logging.getLogger(__name__)


def notify_transfer_completed(txn) -> None:
    _enqueue(txn, template=Template.TRANSFER_COMPLETED)


def notify_transfer_refunded(txn) -> None:
    _enqueue(txn, template=Template.TRANSFER_REFUNDED)


def notify_claim_answered(txn) -> None:
    """Avis de reponse a une reclamation, une seule fois par transfert.

    La contrainte (transaction, modele, canal) porte cette unicite : les
    reponses suivantes ne declenchent rien, et c'est voulu -- le cout
    serait non borne, et le site reste le canal principal.
    """
    _enqueue(txn, template=Template.CLAIM_ANSWERED)


def language_for(customer) -> str:
    """Langue de la derniere connexion, a defaut celle du site."""
    if customer is not None and customer.language:
        return customer.language
    return settings.LANGUAGE_CODE


def render_body(template: str, txn, *, language: str) -> str:
    # override() est indispensable : sans lui, le worker rendrait le texte
    # dans la langue laissee active par le dernier appel de ce thread.
    with translation.override(language):
        return messages.RENDERERS[template](txn)


def _enqueue(txn, *, template: str) -> None:
    """Cree la notification et programme l'envoi APRES le commit.

    Ne leve jamais. Le point de sauvegarde (atomic imbrique) est
    indispensable : une erreur SQL non rattrapee ferait avorter la
    transaction qui vient d'ecrire le reglement.
    """
    try:
        # L'expediteur, c'est le titulaire du compte : son numero a recu un
        # code, il est verifie. sender_phone n'est rempli qu'en USSD.
        customer = txn.customer if txn.customer_id else None
        recipient = customer.phone if customer else ""
        language = language_for(customer)
        with db_transaction.atomic():
            note, created = Notification.objects.get_or_create(
                transaction=txn,
                template=template,
                channel=Channel.SMS,
                defaults={
                    "customer": customer,
                    "recipient": recipient,
                    "language": language,
                    "body": render_body(template, txn, language=language) if recipient else "",
                    "status": Status.PENDING if recipient else Status.SKIPPED,
                },
            )
        if not created or not recipient:
            return
        # on_commit : rien n'est mis en file si la transaction de base est
        # annulee -- un « transfert livre » pour un reglement qui n'a pas
        # eu lieu serait pire que pas de SMS du tout.
        db_transaction.on_commit(lambda: _publish(note.pk))
    except Exception:
        logger.exception("Notification %s non programmee pour %s", template, txn.reference)


def _publish(notification_id: int) -> None:
    """Met la notification en file. NE LEVE JAMAIS.

    Ce code tourne dans un rappel on_commit, donc APRES que la base a
    valide le reglement. Une exception ici -- courtier injoignable,
    resolution DNS en echec -- remonte jusqu'a la vue et affiche une
    erreur a l'operateur alors que l'argent est deja parti. Il relancerait
    alors un decaissement deja fait.

    La ligne reste en base au statut « a envoyer » : le balayage
    periodique la reprendra.
    """
    from .tasks import send_notification

    try:
        send_notification.apply_async((notification_id,), expires=settings.NOTIFICATIONS["MAX_AGE_SECONDS"])
    except Exception:
        logger.exception("Notification %s non mise en file : elle sera reprise par le balayage", notification_id)


def claim(notification_id: int) -> Notification | None:
    """Reserve la notification pour envoi. None s'il n'y a rien a faire.

    Verrouille la ligne, verifie qu'elle est bien a envoyer, incremente le
    compteur de tentatives, puis RELACHE le verrou : on ne tient jamais un
    verrou de ligne pendant un appel reseau.
    """
    with db_transaction.atomic():
        note = Notification.objects.select_for_update().filter(pk=notification_id).first()
        if note is None or note.status != Status.PENDING:
            return None
        age = (timezone.now() - note.created_at).total_seconds()
        if age > settings.NOTIFICATIONS["MAX_AGE_SECONDS"]:
            _finish(note, status=Status.SKIPPED, error_code="TOO_LATE")
            return None
        if note.attempts >= settings.NOTIFICATIONS["MAX_ATTEMPTS"]:
            _finish(note, status=Status.FAILED, error_code="TOO_MANY_ATTEMPTS")
            return None
        note.attempts += 1
        note.save(update_fields=["attempts", "updated_at"])
        return note


def deliver(note: Notification) -> str:
    """Envoie le SMS et ecrit son issue. Retourne le statut atteint.

    Leve TwilioRateLimited / TwilioUnavailable pour que la tache decide
    d'une reprise : ce sont les seuls cas ou l'issue est indeterminee.
    """
    try:
        message_id, provider_status = sms.send(
            phone=note.recipient, body=note.body, idempotency_key=note.idempotency_key
        )
    except sms.SMSNotConfigured as exc:
        # Permanent : sans expediteur, rejouer ne sert a rien.
        return _finish(note, status=Status.SKIPPED, error_code="SMS_NOT_CONFIGURED", error=str(exc))
    except (tw.TwilioRateLimited, tw.TwilioUnavailable):
        _release(note)
        raise
    except tw.TwilioError as exc:
        # Numero invalide, STOP, region fermee : un 4xx restera un 4xx.
        logger.error("SMS %s definitivement refuse pour %s : %s", note.template, note.transaction_id, exc)
        return _finish(note, status=Status.FAILED, error_code=str(exc.code or exc.status or "TWILIO"), error=str(exc))

    note.provider_message_id = message_id
    note.sent_at = timezone.now()
    logger.info(
        "SMS %s envoye a %s (%s) : %s",
        note.template,
        _masked(note.recipient),
        provider_status,
        note.transaction_id,
    )
    return _finish(note, status=Status.SENT)


def _finish(note: Notification, *, status: str, error_code: str = "", error: str = "") -> str:
    note.status = status
    note.error_code = error_code
    note.error_message = error
    note.save(
        update_fields=["status", "error_code", "error_message", "provider_message_id", "sent_at", "updated_at"]
    )
    return status


def _release(note: Notification) -> None:
    """Rend la notification a la file pour la prochaine tentative."""
    Notification.objects.filter(pk=note.pk).update(status=Status.PENDING, updated_at=timezone.now())


def _masked(phone: str) -> str:
    """509…3456 : le numero complet reste en base, pas dans les journaux."""
    return f"{phone[:3]}…{phone[-4:]}" if len(phone) > 7 else phone
