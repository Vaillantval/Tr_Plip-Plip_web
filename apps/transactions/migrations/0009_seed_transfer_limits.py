from decimal import Decimal

from django.db import migrations

#: Plafonds de depart, en HTG. Prudents tant que la recette en production
#: n'est pas faite : un seul transfert au plafond par transaction (50 000)
#: et par jour. Le superadmin les releve depuis la console.
DAILY_CAP = "50000"
MONTHLY_CAP = "200000"


def seed(apps, schema_editor):
    Policy = apps.get_model("transactions", "TransferLimitPolicy")
    Policy.objects.get_or_create(
        pk=1,
        defaults={"daily_cap": Decimal(DAILY_CAP), "monthly_cap": Decimal(MONTHLY_CAP)},
    )


class Migration(migrations.Migration):
    dependencies = [("transactions", "0008_transfer_limits")]

    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
