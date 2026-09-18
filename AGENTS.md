# À lire avant d'écrire du code

Plip-Plip déplace l'argent réel de gens réels. Ce document liste ce qu'on ne
peut **pas** deviner en lisant le code, et ce que coûte chaque règle violée.

Trois documents, sans recouvrement. Celui-ci **renvoie** aux deux autres, il
ne les recopie pas :

| Fichier | Pour qui | Quand y aller |
|---|---|---|
| [`README.md`](README.md) | qui découvre le projet | architecture, démarrage, déploiement, tarification |
| [`RESTAURATION.md`](RESTAURATION.md) | qui est devant une panne | sauvegardes et restauration, une seule fois, à 2 h du matin |
| `AGENTS.md` (ici) | qui va modifier le code | les invariants et les pièges |

**Si une règle de cette page n'est gardée par aucun test, c'est un manque :
signale-le.** Un test qui échoue vaut mieux qu'une ligne de markdown que
personne ne lit. Chaque fois qu'une règle peut devenir un test, elle doit le
devenir.

---

## Le projet en trois phrases

Plateforme d'interopérabilité **MonCash ↔ NatCash** : on encaisse chez un
portefeuille, on reverse sur l'autre. Django 5.2, Postgres en production,
SQLite en développement, Celery + Redis pour les décaissements.

Tout l'argent passe par l'API **plopplop**. `apps/providers/plopplop` est le
seul module autorisé à lui parler ; `apps/providers/twilio` le seul autorisé
à parler à Twilio.

La moitié de l'architecture s'explique par **une seule contrainte** : plopplop
impose **120 secondes entre deux retraits, par IP** (`PAYOUT_COOLDOWN_SECONDS`,
réglé à 125 par prudence). Cela plafonne la plateforme à ~28 décaissements par
heure. D'où la file d'attente, le verrou unique, le worker de décaissement
dédié à concurrence 1, le contrôle d'admission et les estimations d'attente.
**Pas d'environnement de test chez plopplop, pas de webhooks** : le premier
déploiement parle à la production, et les paiements se confirment par polling.

---

## Les invariants intouchables

### 1. `state` ne s'assigne que dans `Transaction.transition()`

Aucune vue, aucun service, aucune tâche n'écrit `txn.state = …`.
`transition()` verrouille la ligne, valide la transition contre la machine à
états et journalise un `TransactionEvent`. Un constat qui ne tranche rien
passe par `record_observation()`.

> Gardé par `tests/test_console.py::test_state_is_only_assigned_inside_transactions_models`.
> Le test inspecte le code source : il refuse même un attribut nommé `state`
> sur une exception. Choisis un autre nom.

### 2. Cinq modèles sont append-only

`JournalEntry`, `LedgerLine`, `TransactionEvent`, `AuditLog`, `ClaimMessage`.
Leur `save()` lève `RuntimeError` si `pk` est déjà posé, leur `delete()` lève
toujours. On corrige une écriture par une **contre-écriture**, jamais en la
modifiant.

Trois conséquences qui surprennent :

- **`loaddata` est inutilisable** sur ces tables : le désérialiseur Django
  pose le pk puis appelle `save()`. Passer par `bulk_create`, qui ne passe pas
  par `save()` — c'est ce que fait `apps/backup/restore.py`.
- `QuerySet.delete()` en masse **contourne** la garde. Les FK `PROTECT`
  restent la seule protection réelle.
- `ledger.post()` écrit ses lignes en `bulk_create` : la garde de `LedgerLine`
  n'est donc jamais déclenchée par le chemin normal.

### 3. La partie double est toujours équilibrée

`ledger.post()` refuse une écriture dont les lignes ne somment pas à zéro.
Sous SQLite, une somme d'agrégat peut donner `-9e-14` : utiliser
`_sum_to_cents` — qui vit dans `apps/ledger/models.py`, **pas** dans
`services.py`.

Aucune contrainte d'équilibre n'existe en base : c'est applicatif.
`manage.py verify_data` est le contrôle global.

### 4. Toute action console sur l'argent est gardée et auditée

`@require_acting_role(...)` ou `@require_superadmin(...)`, jamais un simple
`login_required`. Le décorateur écrit une ligne d'`AuditLog` **avant**
d'exécuter la vue ; l'issue d'une tentative autorisée qui échoue est une
**seconde** ligne, via `audit_failure()`. Les deux sont conservées.

Sur ces lignes, `allowed` signifie **autorisé**, pas **réussi**.

### 5. Tout nouveau modèle entre dans les sauvegardes

Ajouter une entrée dans `apps/backup/schema.py` : soit dans `TABLES` (à la
bonne place, l'ordre est celui des dépendances `PROTECT`), soit dans
`EXCLUDED` **avec la raison écrite**. Renseigner aussi `FOREIGN_KEYS` et
`OPERATOR_KEYS` si le modèle en a.

> Gardé par `tests/test_backup.py::test_every_model_is_either_saved_or_deliberately_excluded`.
> Sans cela, un modèle sort des sauvegardes en silence.

### 6. Jamais de `|safe` ni de `mark_safe`

Il n'y en a zéro dans le dépôt, et c'est gelé. Le contenu saisi par un client
s'affiche échappé, toujours.

> Gardé par `tests/test_claims.py::test_no_template_marks_claim_content_as_safe`,
> qui balaie tout `apps/`. Évite d'écrire la chaîne littérale dans un
> commentaire : le test la verrait.

### 7. Les services métier restent appelables sans requête HTTP

L'API publique servira une application mobile. Un service reçoit le client et
des valeurs simples, et lève des **exceptions typées qui portent le message
destiné au client**. Il ne formate rien, ne renvoie pas de réponse HTTP, ne
touche ni à `request` ni à la session. Le site et l'API le rendent chacun à
leur façon.

> Gardé, pour les réclamations, par `tests/test_claims.py::test_services_take_no_request_and_no_http_object`.

---

## Les pièges qui coûtent une heure

### `on_commit` dans les tests

`pytest-django` n'engage jamais la transaction du test : un rappel
`transaction.on_commit()` ne s'exécute pas tout seul. Il faut
`django_capture_on_commit_callbacks(execute=True)`.

**Et l'ordre compte.** La capture exécute les rappels en **sortant** de son
bloc. Un `mock.patch` ouvert à l'intérieur est déjà retiré à ce moment-là — et
le test part **vraiment** chez Twilio. Patcher à l'extérieur :

```python
with mock.patch(SEND) as send, capture(execute=True):
    ...
```

Le motif est écrit une fois, dans le `sending()` de `tests/test_notifications.py`.

### Les fichiers `.po`

- **Ne jamais les écrire par heredoc** : un niveau d'échappement est consommé,
  les `\n` deviennent de vraies fins de ligne et `msgfmt` refuse le fichier.
  Les écrire directement, ou par un script depuis un fichier.
- **Ne jamais lancer `msgattrib` sur un catalogue neuf** dont l'en-tête porte
  encore `charset=CHARSET` : il mange les accents sans rien dire.
- **Aucune entrée ne reste `fuzzy`.** gettext devine une traduction à partir
  d'une chaîne voisine — il a déjà rendu « trop de réclamations » par « twòp
  transfè ». Une traduction devinée n'est pas une traduction.
- Régénérer depuis la racine, en excluant les apps sans catalogue, sinon
  `makemessages` s'arrête sur le premier `__init__.py` orphelin.

### Les SMS n'ont jamais d'accent

Un seul caractère accentué fait basculer le message en **UCS-2** : 70
caractères par segment au lieu de 160. Le message coûte alors **le double**, à
chaque envoi, pour toujours. Cela vaut dans les trois langues.

Le montant s'écrit avec une **espace ordinaire**, jamais l'espace fine
insécable du site : elle n'existe pas dans le jeu GSM-7 et suffit à elle seule
à faire basculer le message.

> Gardé par `tests/test_notifications.py` et `tests/test_claims.py`.

---

## Les langues

| Où | Accents | Règle |
|---|---|---|
| Commentaires et docstrings | **non** | convention du dépôt, suivre l'existant |
| Messages de commit | **non** | idem |
| Textes d'écran (site, console) | **oui** | français correct |
| SMS | **jamais** | voir ci-dessus, c'est une question de coût |

Le site parle **français, créole, anglais**. Le créole est la langue de la
quasi-totalité des utilisateurs.

**Aucune chaîne du chemin de l'argent ne peut attendre sa traduction créole.**
Montants, frais, confirmation, erreurs, statut, réclamations : une chaîne vide
fait échouer les tests. Seules celles présentes uniquement dans `base.html`,
`login.html` et `login_code.html` peuvent être listées dans
`apps/web/locale/ht/A_TRADUIRE.md`.

> Gardé par `tests/test_web.py::test_money_path_strings_can_never_wait_for_creole`.

Ne traduis pas le créole toi-même. Prépare la liste des `msgid` exacts, avec
pour chacun sa nature et le moment où le client le voit, et attends la
traduction.

---

## Travailler sur ce dépôt

```bash
.venv/Scripts/python.exe -m pytest -q                               # 400 tests
.venv/Scripts/python.exe manage.py check --settings=config.settings.dev
.venv/Scripts/python.exe manage.py makemigrations --check --dry-run --settings=config.settings.dev
```

`config.settings.dev` (SQLite) en local, `config.settings.prod` (Postgres) en
production. Il n'y a **pas de `conftest.py`** : la fixture `books` est
dupliquée dans chaque fichier de test, et c'est assumé.

Commandes de gestion : `predeploy` (joué au déploiement : `check --deploy`,
`migrate`, `init_ledger`, `init_superadmin`), `init_ledger`,
`init_superadmin`, `export_data`, `verify_data`, `import_data`.

### Commits

- **Un commit par tâche.** Un correctif trouvé en chemin part dans son propre
  commit, avant ou après — enterré dans un commit plus gros, personne ne le
  retrouvera.
- Le message dit **pourquoi**, pas quoi : le diff dit déjà quoi. Première
  ligne courte, sans accents, puis le raisonnement.
- **Vérifier le commit seul** avant de le déclarer bon :
  `git worktree add --detach <dossier> <sha>` puis lancer les tests dedans. Un
  commit qui ne passe qu'accompagné des suivants n'est pas un commit.

### Ne jamais pousser sans qu'on le demande

Committer, oui. `git push` seulement sur demande explicite. Même chose pour
tout ce qui sort de la machine : déploiement, message, publication.

---

## Ce qui n'est pas dans le code

- **Les sauvegardes Railway ne protègent pas de la perte de Railway.** D'où
  `export_data`, dont le résultat doit vivre ailleurs. Voir
  [`RESTAURATION.md`](RESTAURATION.md).
- Un export contient les **numéros de téléphone et les montants de tous les
  clients**. Il ne part jamais dans git — `.gitignore` l'en empêche, ne le
  contourne pas.
- La configuration des sauvegardes Railway, du PITR et des domaines est du
  ressort de l'exploitant. Ne cherche pas à la faire depuis le code : dis
  exactement quoi activer, et où.
