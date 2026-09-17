from __future__ import annotations

import secrets
from decimal import Decimal

from django.conf import settings
from django.db import models, transaction as db_transaction
from django.utils import timezone

from .states import LIABILITY_STATES, State, check

REFERENCE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def make_reference() -> str:
    """Reference unique Plip-Plip, lisible a voix haute au telephone.

    Pas de 0/O ni 1/I : ces references sont dictees au support par des
    utilisateurs qui les lisent sur un SMS.
    """
    body = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(8))
    return f"PP-{timezone.now():%y%m}-{body}"


class Wallet(models.TextChoices):
    MONCASH = "moncash", "MonCash"
    NATCASH = "natcash", "NatCash"
    KASHPAW = "kashpaw", "Kashpaw"
    CARTE = "carte", "Carte bancaire"


#: Seuls MonCash et NatCash acceptent un decaissement. Kashpaw et carte
#: ne peuvent servir que de source.
PAYOUT_CAPABLE = (Wallet.MONCASH, Wallet.NATCASH)


class WalletSetting(models.Model):
    """Ouverture d'un moyen de paiement, reglee par le superadmin.

    Deux interrupteurs par portefeuille : en entree (encaissement) et en
    sortie (decaissement). La sortie n'existe que pour PAYOUT_CAPABLE :
    c'est une capacite de plopplop, pas un reglage.

    Ne concerne que les NOUVELLES transactions. Une transaction deja
    encaissee est une dette : fermer la sortie ne bloque pas son
    decaissement.

    Pas de ligne pour un portefeuille = ferme dans les deux sens.
    """

    wallet = models.CharField(max_length=16, choices=Wallet.choices, unique=True)
    payment_enabled = models.BooleanField(default=False)
    payout_enabled = models.BooleanField(default=False)

    # Tarif CLIENT : ce que Plip-Plip facture, en taux decimal (0.03 = 3 %).
    # payment_fee_rate s'applique quand le portefeuille est la source,
    # payout_fee_rate quand il est la destination. Base : montant net.
    payment_fee_rate = models.DecimalField(max_digits=6, decimal_places=4, default=0)
    payout_fee_rate = models.DecimalField(max_digits=6, decimal_places=4, default=0)
    # Cout PLOPPLOP : ce que plopplop retient, pour les estimations
    # (grand livre, couverture, marge). Encaissement : base = montant paye.
    # Retrait : base = montant net (doc : fee 12,5 sur 500 en NatCash).
    # Le montant reel retourne par l'API prime toujours sur l'estimation.
    payment_cost_rate = models.DecimalField(max_digits=6, decimal_places=4, default=0)
    payout_cost_rate = models.DecimalField(max_digits=6, decimal_places=4, default=0)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("wallet",)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(payout_enabled=False) | models.Q(wallet__in=[w.value for w in PAYOUT_CAPABLE]),
                name="payout_only_for_payout_capable_wallets",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_wallet_display()} (entree={self.payment_enabled}, sortie={self.payout_enabled})"


class TransferLimitPolicy(models.Model):
    """Plafonds cumules par client. Une seule ligne (pk=1).

    Fenetres GLISSANTES : elles remontent depuis maintenant, elles ne se
    remettent pas a zero a minuit ni le 1er du mois. Verifiees a la
    CREATION uniquement : une transaction encaissee est une dette, aucun
    plafond ne bloque jamais son decaissement.

    Le plafond par transaction (PRICING["MAX_NET_AMOUNT"]) reste separe :
    il protege d'une faute de frappe, ceux-ci d'un cumul.
    """

    daily_cap = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("50000"))
    monthly_cap = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("200000"))
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(daily_cap__gt=0) & models.Q(monthly_cap__gte=models.F("daily_cap")),
                name="monthly_cap_covers_daily_cap",
            )
        ]

    def __str__(self) -> str:
        return f"Plafonds {self.daily_cap} HTG/jour, {self.monthly_cap} HTG/30 jours"


class PricingPolicy(models.Model):
    """Reglages de tarification globaux. Une seule ligne (pk=1).

    `reviewed_at` reste vide tant qu'un superadmin n'a pas enregistre la
    tarification depuis la console : le tableau de bord le lui signale.
    """

    platform_fee_rate = models.DecimalField(max_digits=6, decimal_places=4, default=0)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Commission plateforme {self.platform_fee_rate}"


class TransactionQuerySet(models.QuerySet):
    def liabilities(self):
        """Transactions ou nous detenons les fonds du client."""
        return self.filter(state__in=list(LIABILITY_STATES))

    def awaiting_payment(self):
        return self.filter(state=State.AWAITING_PAYMENT)

    def payable(self):
        # `id` departage deux confirmations simultanees : l'ordre du worker
        # et celui des estimations (apps.transactions.queue) sont identiques.
        return self.filter(state=State.PAYOUT_QUEUED).order_by("payment_confirmed_at", "id")

    def with_state_since(self):
        """Annote `state_since` : entree dans l'etat courant, lue dans le
        journal des evenements (updated_at bouge a chaque sauvegarde).
        """
        entered = (
            TransactionEvent.objects.filter(
                transaction=models.OuterRef("pk"), to_state=models.OuterRef("state")
            )
            # Un constat (from_state == to_state) n'est pas une entree dans l'etat.
            .exclude(from_state=models.F("to_state"))
            .order_by("-created_at", "-id")
            .values("created_at")[:1]
        )
        return self.annotate(state_since=models.Subquery(entered))


class Transaction(models.Model):
    """Une conversion wallet -> wallet.

    `state` n'est jamais assigne directement : passer par transition().
    """

    reference = models.CharField(max_length=32, unique=True, default=make_reference, editable=False)
    state = models.CharField(max_length=32, choices=State.choices, default=State.CREATED, db_index=True)

    source_wallet = models.CharField(max_length=16, choices=Wallet.choices)
    destination_wallet = models.CharField(max_length=16, choices=Wallet.choices)
    sender_phone = models.CharField(max_length=16, blank=True)
    recipient_phone = models.CharField(max_length=16)

    # Devis fige a la creation. On ne le recalcule jamais : si les taux
    # changent, les transactions en cours gardent leur devis d'origine.
    net_amount = models.DecimalField(max_digits=12, decimal_places=2)
    fee_in = models.DecimalField(max_digits=12, decimal_places=2)
    fee_out = models.DecimalField(max_digits=12, decimal_places=2)
    fee_platform = models.DecimalField(max_digits=12, decimal_places=2)
    total_charged = models.DecimalField(max_digits=12, decimal_places=2)
    # Couts plopplop estimes, figes a la creation avec le devis.
    provider_fee_in = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    provider_fee_out_estimate = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Cote encaissement
    payment_provider_id = models.CharField(max_length=64, blank=True)
    payment_redirect_url = models.URLField(blank=True, max_length=500)
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)
    # Montant que plopplop declare avoir encaisse. Vide s'il ne l'a pas
    # communique : la transaction est alors bloquee (AMOUNT_UNVERIFIED).
    payment_amount_received = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    payment_expires_at = models.DateTimeField(null=True, blank=True)
    last_polled_at = models.DateTimeField(null=True, blank=True)
    poll_count = models.PositiveIntegerField(default=0)

    # Cote decaissement
    payout_reference = models.CharField(max_length=40, blank=True, db_index=True)
    payout_provider_id = models.CharField(max_length=64, blank=True)
    payout_api_reference = models.CharField(max_length=64, blank=True)
    payout_fee_actual = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    payout_attempts = models.PositiveIntegerField(default=0)
    payout_completed_at = models.DateTimeField(null=True, blank=True)
    # Promesse de delai faite au client (apps.transactions.queue) : borne
    # haute annoncee, fixee une fois ; retiree si la realite la depasse.
    payout_eta_deadline = models.DateTimeField(null=True, blank=True)
    payout_eta_withdrawn = models.BooleanField(default=False)

    failure_code = models.CharField(max_length=64, blank=True)
    failure_message = models.TextField(blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions"
    )
    # Client de l'API a l'origine de la transaction. PROTECT : un client
    # qui a transige ne se supprime pas, il se desactive.
    customer = models.ForeignKey(
        "accounts.Customer", null=True, blank=True, on_delete=models.PROTECT, related_name="transactions"
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = TransactionQuerySet.as_manager()

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["customer", "state", "payment_confirmed_at"], name="txn_customer_window_idx"),
            models.Index(fields=["state", "created_at"]),
            models.Index(fields=["recipient_phone"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} ({self.get_state_display()})"

    # ------------------------------------------------------------------
    @property
    def margin_estimate(self) -> Decimal:
        """Marge estimee : frais factures moins couts plopplop.

        Frais de retrait reels des qu'ils sont connus, estimation figee
        sinon. Negative si les couts depassent le devis.
        """
        collected = self.fee_in + self.fee_out + self.fee_platform
        payout_cost = self.payout_fee_actual if self.payout_fee_actual is not None else self.provider_fee_out_estimate
        return collected - self.provider_fee_in - payout_cost

    @property
    def is_liability(self) -> bool:
        return self.state in LIABILITY_STATES

    def build_payout_reference(self) -> str:
        """Reference envoyee a plopplop pour le retrait.

        Suffixee par le numero de tentative : plopplop bloque en 409 une
        reference deja utilisee, et la doc ne precise pas si une
        reference liberee apres echec est reutilisable. On ne prend pas
        le risque -- chaque tentative porte sa propre reference, et le
        lien avec la transaction reste assure par le prefixe.
        """
        return f"{self.reference}-W{self.payout_attempts + 1}"

    @db_transaction.atomic
    def transition(self, target: str, *, actor=None, note: str = "", data: dict | None = None) -> "Transaction":
        """Unique point d'entree pour changer d'etat.

        Verrouille la ligne, valide la transition, journalise. Toute
        tentative de transition interdite leve IllegalTransition avant
        toute ecriture.
        """
        current = Transaction.objects.select_for_update().get(pk=self.pk)
        check(current.state, target)

        previous = current.state
        current.state = target
        if target == State.PAYMENT_CONFIRMED and current.payment_confirmed_at is None:
            current.payment_confirmed_at = timezone.now()
        if target == State.COMPLETED and current.payout_completed_at is None:
            current.payout_completed_at = timezone.now()
        current.save(update_fields=["state", "payment_confirmed_at", "payout_completed_at", "updated_at"])

        TransactionEvent.objects.create(
            transaction=current,
            from_state=previous,
            to_state=target,
            actor=actor,
            note=note,
            data=data or {},
        )
        self.state = target
        return current

    @db_transaction.atomic
    def record_observation(self, *, actor=None, note: str = "", data: dict | None = None) -> "TransactionEvent":
        """Journalise un constat SANS changer d'etat (from_state == to_state).

        Pour les verifications qui ne tranchent rien mais dont la trace
        compte pour la suite, comme un 404 sur un PAYOUT_UNKNOWN. Ne touche
        jamais a `state` : seul transition() le fait.
        """
        current = Transaction.objects.select_for_update().get(pk=self.pk)
        return TransactionEvent.objects.create(
            transaction=current,
            from_state=current.state,
            to_state=current.state,
            actor=actor,
            note=note,
            data=data or {},
        )


class TransactionEvent(models.Model):
    """Journal append-only. Aucune mise a jour, aucune suppression.

    C'est la timeline affichee dans la console et la piece justificative
    en cas de reclamation.
    """

    transaction = models.ForeignKey(Transaction, on_delete=models.PROTECT, related_name="events")
    from_state = models.CharField(max_length=32, blank=True)
    to_state = models.CharField(max_length=32)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="transaction_events"
    )
    note = models.TextField(blank=True)
    data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at", "id")

    def __str__(self) -> str:
        return f"{self.transaction.reference}: {self.from_state} -> {self.to_state}"

    def save(self, *args, **kwargs):
        if self.pk is not None:
            raise RuntimeError("TransactionEvent est append-only")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("TransactionEvent est append-only")
