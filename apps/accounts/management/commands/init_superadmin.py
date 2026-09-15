"""Cree ou met a jour le superadmin defini par les variables d'environnement.

Execute a chaque deploiement, dans `manage.py predeploy`.

Variables (service web sur Railway) :
  SUPERADMIN_EMAIL     defaut : info@plip.ht
  SUPERADMIN_USERNAME  defaut : SUPERADMIN_EMAIL
  SUPERADMIN_PASSWORD  vide : rien n'est fait

Les variables font foi. Modifier SUPERADMIN_PASSWORD dans Railway
redeploie le service, et le mot de passe est aligne a ce deploiement.
Consequence : un mot de passe change depuis la console est remplace au
deploiement suivant. Changer SUPERADMIN_USERNAME cree un nouveau compte ;
l'ancien reste, a desactiver a la main.

N'echoue jamais le deploiement : un mot de passe absent ou trop faible est
signale dans les logs et ignore, le compte existant reste inchange.
Chaque creation ou modification est inscrite au journal d'audit (sans le
mot de passe).
"""

from __future__ import annotations

import os

from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import AuditLog, Role

DEFAULT_EMAIL = "info@plip.ht"

#: Console qui touche a l'argent : exigences plus fortes que les defauts Django.
PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


class Command(BaseCommand):
    help = "Cree ou met a jour le superadmin depuis SUPERADMIN_USERNAME / SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD."

    def handle(self, *args, **options):
        email = os.environ.get("SUPERADMIN_EMAIL", "").strip() or DEFAULT_EMAIL
        username = os.environ.get("SUPERADMIN_USERNAME", "").strip() or email
        password = os.environ.get("SUPERADMIN_PASSWORD", "")

        if not password:
            self.stdout.write(self.style.WARNING("SUPERADMIN_PASSWORD vide : superadmin ni cree ni modifie."))
            return

        User = get_user_model()
        with transaction.atomic():
            existing = User.objects.select_for_update().filter(username=username).first()
            user = existing or User(username=username, email=email)
            try:
                password_validation.validate_password(
                    password,
                    user=user,
                    password_validators=password_validation.get_password_validators(PASSWORD_VALIDATORS),
                )
            except ValidationError as exc:
                self.stderr.write(
                    self.style.ERROR(
                        f"SUPERADMIN_PASSWORD refuse ({' '.join(exc.messages)}) : superadmin ni cree ni modifie."
                    )
                )
                return

            changes = [] if existing else ["created"]
            wanted = {"email": email, "role": Role.SUPERADMIN, "is_staff": True, "is_superuser": True, "is_active": True}
            for field, value in wanted.items():
                if getattr(user, field) != value:
                    setattr(user, field, value)
                    if existing:
                        changes.append(field)
            if not existing or not user.check_password(password):
                user.set_password(password)
                if existing:
                    changes.append("password")

            if not changes:
                self.stdout.write(f"Superadmin {username} deja a jour.")
                return

            user.save()
            AuditLog.objects.create(
                user=None, action="superadmin.init", target=username, allowed=True, detail={"changes": changes}
            )

        verb = "cree" if not existing else f"mis a jour ({', '.join(changes)})"
        self.stdout.write(self.style.SUCCESS(f"Superadmin {username} {verb}."))
