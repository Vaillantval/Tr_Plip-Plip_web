"""Ce qu'une sauvegarde contient, dans quel ordre, et ce qu'elle exclut.

Source unique : l'export, l'import et la verification lisent tous cette
liste. Ajouter un modele au projet sans l'ajouter ici le laisse hors des
sauvegardes en silence -- c'est exactement ce que verifie
test_every_model_is_either_saved_or_deliberately_excluded.
"""

from __future__ import annotations

#: Version du format de fichier. A incrementer si la structure de
#: l'enveloppe change, pas quand un champ de modele bouge : les champs
#: sont portes par le serialiseur Django, qui tolere l'absence.
FORMAT_VERSION = 1

#: Tables du fichier principal, dans l'ORDRE D'IMPORT.
#: Cet ordre est impose par les cles etrangeres PROTECT : une ligne ne
#: peut entrer que si ce qu'elle reference est deja la.
TABLES = (
    "accounts.customer",
    "ledger.ledgeraccount",
    "transactions.walletsetting",
    "transactions.pricingpolicy",
    "transactions.transferlimitpolicy",
    "transactions.transaction",
    "transactions.transactionevent",
    "ledger.journalentry",
    "ledger.ledgerline",
    "api.idempotencykey",
    "treasury.floatsnapshot",
    "treasury.floatalert",
    "accounts.auditlog",
)

#: Second fichier, TOUJOURS chiffre : les comptes qui peuvent rembourser
#: et recharger le float. Mots de passe hachees -- inexploitables tels
#: quels, mais un fichier vole se casse hors ligne, tranquillement.
OPERATOR_TABLES = ("accounts.user",)

#: Hors sauvegarde, volontairement. La raison est ecrite ici pour qu'on
#: ne « repare » pas cet oubli sans la lire.
EXCLUDED = {
    "accounts.customertoken": (
        "Jetons de session clients. Un export qui les contient donne acces "
        "aux comptes des utilisateurs, et il vivra sur des cles USB. Apres "
        "restauration, les clients se reconnectent par SMS."
    ),
    "notifications.notification": (
        "Journal d'envoi de SMS : rien d'irremplacable, et le contenu est "
        "reconstructible depuis la transaction."
    ),
    "sessions.session": "Sessions Django. Meme raison que les jetons clients.",
    "admin.logentry": "Journal de l'admin Django. L'audit metier est dans accounts.auditlog.",
    "auth.permission": "Recree par migrate.",
    "auth.group": "Aucun groupe utilise : les droits viennent de User.role.",
    "contenttypes.contenttype": "Recree par migrate.",
}

#: Cles etrangeres INTERNES au fichier principal, verifiees sans base de
#: donnees : {table: {champ: table_cible}}.
FOREIGN_KEYS = {
    "transactions.transaction": {"customer": "accounts.customer"},
    "transactions.transactionevent": {"transaction": "transactions.transaction"},
    "ledger.journalentry": {
        "transaction": "transactions.transaction",
        "reverses": "ledger.journalentry",
    },
    "ledger.ledgerline": {
        "entry": "ledger.journalentry",
        "account": "ledger.ledgeraccount",
    },
    "api.idempotencykey": {
        "customer": "accounts.customer",
        "transaction": "transactions.transaction",
    },
    "treasury.floatsnapshot": {"transaction": "transactions.transaction"},
}

#: Cles etrangeres vers les comptes operateurs. Elles vivent dans l'autre
#: fichier : toutes sont en SET_NULL, donc une restauration sans le
#: fichier operateurs les vide au lieu d'echouer. On perd « qui a fait
#: quoi », jamais l'argent.
OPERATOR_KEYS = {
    "transactions.transaction": ("created_by",),
    "transactions.transactionevent": ("actor",),
    "transactions.walletsetting": ("updated_by",),
    "transactions.pricingpolicy": ("reviewed_by",),
    "transactions.transferlimitpolicy": ("updated_by",),
    "ledger.journalentry": ("posted_by",),
    "treasury.floatalert": ("acknowledged_by",),
    "accounts.auditlog": ("user",),
}


def get_model(label: str):
    from django.apps import apps

    app_label, model_name = label.split(".")
    return apps.get_model(app_label, model_name)
