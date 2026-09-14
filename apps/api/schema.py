from drf_spectacular.extensions import OpenApiAuthenticationExtension


class CustomerTokenScheme(OpenApiAuthenticationExtension):
    target_class = "apps.api.authentication.CustomerTokenAuthentication"
    name = "CustomerToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": "Jeton `ppk_...` obtenu par POST /api/v1/auth/otp/verify",
        }
