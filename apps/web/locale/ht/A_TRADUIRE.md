# Créole : chaînes en attente de traduction

Le créole est la langue de la quasi-totalité des utilisateurs. Une chaîne
non traduite rend l'écran incompréhensible pour la majorité d'entre eux :
cette liste est une file de travail à vider, pas une dérogation.

Règles, vérifiées par `tests/test_web.py` :

1. **Le chemin de l'argent n'entre jamais ici.** Montants, frais,
   confirmation de paiement, messages d'erreur, écran de statut : une
   chaîne non traduite dans ces écrans fait échouer les tests, sans
   exception. Seules les chaînes présentes uniquement dans
   `base.html`, `login.html` et `login_code.html` peuvent attendre.
2. **Pas d'entrée obsolète.** Une chaîne listée qui n'existe plus dans le
   catalogue, ou qui est désormais traduite, doit sortir de la liste.
3. **Toute chaîne créole vide doit être listée ici** (et respecter la
   règle 1).

Traduire dans `django.po`, puis `python manage.py compilemessages
--ignore ".venv/*"` et retirer la ligne correspondante ci-dessous.

Une chaîne par ligne, `msgid` exact :

```text
```
