from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def health(request):
    """Healthcheck Railway : le deploiement ne passe en service que si
    l'application repond ET joint la base.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return JsonResponse({"status": "database_unavailable"}, status=503)
    return JsonResponse({"status": "ok"})
