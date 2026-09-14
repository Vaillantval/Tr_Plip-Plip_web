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
