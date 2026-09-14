from django.contrib import admin

from .models import Transaction, TransactionEvent


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """Lecture seule, volontairement.

    L'admin Django reste disponible pour le debug d'urgence mais ne doit
    jamais servir a modifier une transaction : tout changement d'etat
    passe par Transaction.transition(), qui journalise et valide.
    """

    list_display = ("reference", "state", "source_wallet", "destination_wallet", "net_amount", "created_at")
    list_filter = ("state", "source_wallet", "destination_wallet")
    search_fields = ("reference", "recipient_phone", "payment_provider_id")
    readonly_fields = [f.name for f in Transaction._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TransactionEvent)
class TransactionEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "transaction", "from_state", "to_state", "actor")
    readonly_fields = [f.name for f in TransactionEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
