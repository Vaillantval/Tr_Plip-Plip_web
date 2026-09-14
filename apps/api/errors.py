"""Format d'erreur unique de l'API.

    {"error": {"code": "METHOD_DISABLED", "message": "...", "fields": {...}}}

`code` est stable et destine au code client ; `message` est lisible mais
peut changer.
"""

from __future__ import annotations

from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.views import exception_handler


class APIError(exceptions.APIException):
    def __init__(self, code: str, message: str, *, http_status: int = status.HTTP_400_BAD_REQUEST, extra=None):
        super().__init__(detail=message, code=code)
        self.status_code = http_status
        self.error_code = code
        self.extra = extra or {}


DEFAULT_CODES = {
    exceptions.NotAuthenticated: "NOT_AUTHENTICATED",
    exceptions.AuthenticationFailed: "AUTHENTICATION_FAILED",
    exceptions.PermissionDenied: "PERMISSION_DENIED",
    exceptions.NotFound: "NOT_FOUND",
    exceptions.MethodNotAllowed: "METHOD_NOT_ALLOWED",
    exceptions.Throttled: "RATE_LIMITED",
    exceptions.ParseError: "PARSE_ERROR",
    exceptions.UnsupportedMediaType: "UNSUPPORTED_MEDIA_TYPE",
}


def handler(exc, context):
    response = exception_handler(exc, context)
    if response is None:
        return None

    if isinstance(exc, APIError):
        body = {"code": exc.error_code, "message": str(exc.detail), **exc.extra}
    elif isinstance(exc, exceptions.ValidationError):
        body = {"code": "INVALID_REQUEST", "message": "Requete invalide", "fields": exc.detail}
    else:
        code = next((c for cls, c in DEFAULT_CODES.items() if isinstance(exc, cls)), "ERROR")
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        body = {"code": code, "message": detail}
        if isinstance(exc, exceptions.Throttled) and exc.wait is not None:
            body["retry_after"] = int(exc.wait)

    return Response({"error": body}, status=response.status_code, headers=_passthrough_headers(response))


def _passthrough_headers(response):
    return {k: v for k, v in response.items() if k in ("WWW-Authenticate", "Retry-After")}
