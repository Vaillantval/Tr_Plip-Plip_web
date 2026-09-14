from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = "console"

urlpatterns = [
    path("login/", auth_views.LoginView.as_view(template_name="console/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("transactions/", views.transaction_list, name="transactions"),
    path("transactions/<str:reference>/", views.transaction_detail, name="transaction_detail"),
    path("transactions/<str:reference>/retry/", views.payout_retry, name="payout_retry"),
    path("transactions/<str:reference>/verify/", views.payout_verify, name="payout_verify"),
    path("transactions/<str:reference>/refund/", views.transaction_refund, name="transaction_refund"),
    path("queue/", views.payout_queue, name="queue"),
    path("exceptions/", views.exceptions, name="exceptions"),
    path("treasury/", views.treasury_view, name="treasury"),
    path("treasury/topup/", views.float_topup, name="float_topup"),
    path("treasury/alerts/<int:alert_id>/ack/", views.alert_acknowledge, name="alert_acknowledge"),
    path("methods/", views.payment_methods, name="methods"),
    path("methods/<str:wallet>/", views.wallet_availability_update, name="wallet_availability"),
]
