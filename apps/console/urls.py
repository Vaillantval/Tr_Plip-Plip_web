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
    path("queue/", views.payout_queue, name="queue"),
    path("exceptions/", views.exceptions, name="exceptions"),
    path("treasury/", views.treasury, name="treasury"),
]
