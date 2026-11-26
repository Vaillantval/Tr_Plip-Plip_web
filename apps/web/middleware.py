from django.utils.functional import SimpleLazyObject

from .session import get_customer


class CustomerSessionMiddleware:
    """Pose request.customer pour les gabarits, evalue seulement s'il est lu.

    Dans le code Python, utiliser session.get_customer(request) : un objet
    paresseux qui enveloppe None n'est jamais « is None ».
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.customer = SimpleLazyObject(lambda: get_customer(request))
        return self.get_response(request)


#: Politique de securite du contenu du site client. Rien d'exterieur, aucun
#: script ni style ecrit dans la page : une injection HTML ne peut ni
#: executer de code, ni charger une ressource, ni envoyer un formulaire
#: ailleurs. Le JavaScript vit dans static/web/app.js.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)


class ContentSecurityPolicyMiddleware:
    """Pose la CSP sur les pages du site client (espace de noms « web »).

    La console et l'admin ont leurs propres gabarits et n'y sont pas soumis.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        match = getattr(request, "resolver_match", None)
        if match is not None and match.namespace == "web":
            response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        return response
