from django.conf import settings
from django.urls import path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from . import views

app_name = "api"

urlpatterns = [
    path("auth/otp/request", views.OTPRequestView.as_view(), name="otp_request"),
    path("auth/otp/verify", views.OTPVerifyView.as_view(), name="otp_verify"),
    path("auth/logout", views.LogoutView.as_view(), name="logout"),
    path("me", views.MeView.as_view(), name="me"),
    path("meta", views.MetaView.as_view(), name="meta"),
    path("quotes", views.QuoteView.as_view(), name="quotes"),
    path("transfers", views.TransferListCreateView.as_view(), name="transfers"),
    path("transfers/<str:reference>", views.TransferDetailView.as_view(), name="transfer_detail"),
]

if settings.API_DOCS_ENABLED:
    urlpatterns += [
        path("schema", SpectacularAPIView.as_view(), name="schema"),
        path("docs", SpectacularSwaggerView.as_view(url_name="api:schema"), name="docs"),
    ]
