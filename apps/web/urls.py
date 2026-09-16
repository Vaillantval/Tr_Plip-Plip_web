from django.urls import path

from . import views

app_name = "web"

urlpatterns = [
    path("", views.home, name="home"),
    path("envoyer/devis/", views.quote, name="quote"),
    path("envoyer/", views.send, name="send"),
    path("envoyer/confirmer/", views.confirm, name="confirm"),
    path("transferts/", views.transfers, name="transfers"),
    path("transferts/<str:reference>/", views.transfer_detail, name="transfer_detail"),
    path("transferts/<str:reference>/statut/", views.transfer_status, name="transfer_status"),
    # URL de retour a saisir dans l'espace marchand plopplop.
    path("paiement/retour/", views.payment_return, name="payment_return"),
    path("connexion/", views.login, name="login"),
    path("connexion/code/", views.login_code, name="login_code"),
    path("deconnexion/", views.logout, name="logout"),
]
