from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import AuditLog, User


@admin.register(User)
class PlipUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (("Plip-Plip", {"fields": ("role", "phone")}),)
    list_display = ("username", "email", "role", "is_active")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Lecture seule : le journal d'audit ne s'edite pas."""

    list_display = ("created_at", "user", "action", "target", "allowed")
    list_filter = ("allowed", "action")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
