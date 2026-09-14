"""Limites de debit.

L'envoi de SMS coute de l'argent et attire la fraude au pompage de SMS :
il est limite par numero ET par adresse IP. Les taux sont dans
REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"].
"""

from __future__ import annotations

from rest_framework.throttling import SimpleRateThrottle

from apps.accounts.phone import InvalidPhone, normalize

from .authentication import is_customer


class _PhoneThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        try:
            ident = normalize(request.data.get("phone", ""))
        except InvalidPhone:
            ident = f"ip:{self.get_ident(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


class OTPRequestPhoneBurstThrottle(_PhoneThrottle):
    scope = "otp_request_phone_burst"


class OTPRequestPhoneThrottle(_PhoneThrottle):
    scope = "otp_request_phone"


class OTPVerifyPhoneThrottle(_PhoneThrottle):
    scope = "otp_verify_phone"


class _IPThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class OTPIPThrottle(_IPThrottle):
    scope = "otp_ip"


class PublicIPThrottle(_IPThrottle):
    scope = "public_ip"


class _CustomerThrottle(SimpleRateThrottle):
    def get_cache_key(self, request, view):
        if not is_customer(request):
            return self.cache_format % {"scope": self.scope, "ident": f"ip:{self.get_ident(request)}"}
        return self.cache_format % {"scope": self.scope, "ident": request.user.pk}


class TransferCreateThrottle(_CustomerThrottle):
    scope = "transfer_create"


class CustomerReadThrottle(_CustomerThrottle):
    scope = "customer_read"
