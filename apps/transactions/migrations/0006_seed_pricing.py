from decimal import Decimal

from django.db import migrations

#: Tarifs de depart, a valider par le superadmin (reviewed_at vide).
#:   frais client : 3 % par cote, repris de la note conceptuelle ;
#:   couts plopplop au retrait : MonCash 4 %, NatCash 2,5 % (plopplop ;
#:   2,5 % confirme par l'exemple de la doc : fee 12,5 sur 500) ;
#:   couts plopplop a l'encaissement : inconnus, 0 en attendant.
#: wallet: (payment_fee_rate, payout_fee_rate, payment_cost_rate, payout_cost_rate)
INITIAL = {
    "moncash": ("0.03", "0.03", "0", "0.04"),
    "natcash": ("0.03", "0.03", "0", "0.025"),
    "kashpaw": ("0.03", "0", "0", "0"),
    "carte": ("0.03", "0", "0", "0"),
}
PLATFORM_FEE_RATE = "0.03"


def seed(apps, schema_editor):
    WalletSetting = apps.get_model("transactions", "WalletSetting")
    PricingPolicy = apps.get_model("transactions", "PricingPolicy")
    for wallet, (payment_fee, payout_fee, payment_cost, payout_cost) in INITIAL.items():
        WalletSetting.objects.filter(wallet=wallet).update(
            payment_fee_rate=Decimal(payment_fee),
            payout_fee_rate=Decimal(payout_fee),
            payment_cost_rate=Decimal(payment_cost),
            payout_cost_rate=Decimal(payout_cost),
        )
    PricingPolicy.objects.get_or_create(pk=1, defaults={"platform_fee_rate": Decimal(PLATFORM_FEE_RATE)})


class Migration(migrations.Migration):

    dependencies = [
        ('transactions', '0005_pricing_rates_and_provider_costs'),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
