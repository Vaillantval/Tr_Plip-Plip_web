"""Calcul des frais.

Le principe de la note conceptuelle : le BENEFICIAIRE recoit le montant
annonce, l'expediteur paie ce montant plus les frais. Toutes les
formules partent donc du net, jamais du brut.

    net           montant recu par le beneficiaire
    fee_in        frais operateur cote encaissement
    fee_out       frais operateur cote decaissement
    fee_platform  commission Plip-Plip
    total         net + fee_in + fee_out + fee_platform  (debite au payeur)

Les taux ne sont pas ici : le superadmin les regle par portefeuille dans
la console (WalletSetting, PricingPolicy), avec les couts plopplop qui
servent aux estimations. Ce module ne fait que le calcul. La
reconciliation utilise les montants reellement retournes par l'API,
jamais ces taux.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
MIN_NET = Decimal("20")


def round_htg(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Quote:
    net_amount: Decimal
    fee_in: Decimal
    fee_out: Decimal
    fee_platform: Decimal
    total_fees: Decimal
    total_charged: Decimal
    source_wallet: str
    destination_wallet: str

    def as_dict(self) -> dict:
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(self).items()}


class AmountTooSmall(ValueError):
    pass


class AmountTooLarge(ValueError):
    pass


def quote(
    *,
    net_amount: Decimal,
    source_wallet: str,
    destination_wallet: str,
    rates: dict[str, Decimal],
    max_net: Decimal | None = None,
) -> Quote:
    """Devis pour une conversion.

    `rates` attend les cles 'in', 'out' et 'platform' (taux decimaux,
    ex. Decimal('0.03')).
    """
    net = round_htg(Decimal(net_amount))
    if net < MIN_NET:
        raise AmountTooSmall(f"Le montant minimum est de {MIN_NET} HTG")
    if max_net is not None and net > max_net:
        raise AmountTooLarge(f"Le montant maximum est de {max_net} HTG")

    fee_in = round_htg(net * rates["in"])
    fee_out = round_htg(net * rates["out"])
    fee_platform = round_htg(net * rates["platform"])
    total_fees = fee_in + fee_out + fee_platform

    return Quote(
        net_amount=net,
        fee_in=fee_in,
        fee_out=fee_out,
        fee_platform=fee_platform,
        total_fees=total_fees,
        total_charged=net + total_fees,
        source_wallet=source_wallet,
        destination_wallet=destination_wallet,
    )


