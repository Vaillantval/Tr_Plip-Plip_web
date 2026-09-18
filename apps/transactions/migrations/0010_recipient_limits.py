"""Plafonds du beneficiaire : champs, index de fenetre, et normalisation
des numeros deja en base.
"""

from decimal import Decimal

from django.conf import settings
from django.db import migrations, models


def normalise_phones(apps, schema_editor):
    """Ramene les numeros existants au format canonique 509XXXXXXXX.

    Sans cela, 509XXXXXXXX et XXXXXXXX cohabitant en base feraient deux
    beneficiaires distincts, et le plafond se contournerait en changeant le
    format de saisie. La normalisation est desormais imposee a l'ecriture
    (create_transaction) ; celle-ci rattrape le passe.

    Ne leve JAMAIS sur une donnee douteuse : un numero qui ne se normalise
    pas est laisse tel quel. Une migration qui plante sur une ligne
    ancienne bloquerait un deploiement pour rien.

    En production, les deux seuls chemins de creation normalisaient deja :
    l'effet attendu est nul, et c'est precisement ce qu'on veut constater.
    """
    from apps.accounts.phone import InvalidPhone, normalize

    Transaction = apps.get_model("transactions", "Transaction")
    changed = []
    for txn in Transaction.objects.only("id", "recipient_phone", "sender_phone").iterator():
        fields = []
        for field in ("recipient_phone", "sender_phone"):
            raw = getattr(txn, field)
            if not raw:
                continue
            try:
                canonical = normalize(raw)
            except InvalidPhone:
                continue
            if canonical != raw:
                setattr(txn, field, canonical)
                fields.append(field)
        if fields:
            changed.append((txn, fields))
    for txn, fields in changed:
        Transaction.objects.filter(pk=txn.pk).update(**{f: getattr(txn, f) for f in fields})


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_customer_language'),
        ('transactions', '0009_seed_transfer_limits'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name='transaction',
            name='transaction_recipie_2c8334_idx',
        ),
        migrations.AddField(
            model_name='transferlimitpolicy',
            name='recipient_daily_cap',
            field=models.DecimalField(decimal_places=2, default=Decimal('150000'), max_digits=12),
        ),
        migrations.AddField(
            model_name='transferlimitpolicy',
            name='recipient_monthly_cap',
            field=models.DecimalField(decimal_places=2, default=Decimal('600000'), max_digits=12),
        ),
        migrations.AddIndex(
            model_name='transaction',
            index=models.Index(fields=['recipient_phone', 'payment_confirmed_at'], name='txn_recipient_window_idx'),
        ),
        migrations.AddConstraint(
            model_name='transferlimitpolicy',
            constraint=models.CheckConstraint(condition=models.Q(('recipient_daily_cap__gt', 0), ('recipient_monthly_cap__gte', models.F('recipient_daily_cap'))), name='recipient_monthly_cap_covers_daily_cap'),
        ),
        migrations.RunPython(normalise_phones, migrations.RunPython.noop),
    ]
