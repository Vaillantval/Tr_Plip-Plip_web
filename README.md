# Plip-Plip — squelette backend

Plateforme d'interopérabilité MonCash ↔ NatCash, adossée à l'API plopplop
(`https://plopplop.solutionip.app`).

## Démarrage

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # renseigner PLOPPLOP_CLIENT_ID / CLIENT_SECRET
python manage.py migrate
python manage.py init_ledger  # crée les comptes du grand livre
python manage.py createsuperuser
python manage.py runserver
pytest
```

Le CSS de la console est compilé par Tailwind et n'est pas versionné. Sans
cette étape, `/console/` s'affiche sans habillage en local (la production le
compile dans l'image, cf. `Dockerfile`) :

```bash
npm ci
npm run build:css     # ou npm run watch:css pendant le développement
```

Le site client (`/`) est en CSS écrit à la main et ne dépend ni de npm ni de
Tailwind.

**Avant de modifier le code**, lire [`AGENTS.md`](AGENTS.md) : les invariants
du projet et les pièges qui ne se devinent pas à la lecture du dépôt.
En cas de panne, [`RESTAURATION.md`](RESTAURATION.md).

## Structure

| Dossier | Rôle |
|---|---|
| `apps/providers/plopplop` | Seul module qui parle à plopplop. Signature HMAC, 3 étapes de retrait, exceptions typées. |
| `apps/transactions` | Machine à états, tarification, orchestration, tâches Celery. |
| `apps/ledger` | Grand livre en partie double, append-only. |
| `apps/treasury` | Suivi du float, seuils, projection de rupture. |
| `apps/accounts` | Utilisateurs, rôles d'exploitation, journal d'audit. |
| `apps/web` | Site client (`/`) : envoi, paiement, suivi. Français, créole, anglais. |
| `apps/console` | Console d'exploitation (`/console/`, Django templates + HTMX). |
| `apps/api` | API publique v1 (DRF) pour le front web et l'app Flutter. |
| `apps/providers/twilio` | Seul module qui parle à Twilio Verify (codes SMS). |
| `apps/backup` | Export, vérification d'intégrité et restauration. Voir [RESTAURATION.md](RESTAURATION.md). |

## Site client — `/`

Pages Django + HTMX, mobile d'abord, sans dépendance à un CDN (HTMX 2.0.4
est embarqué dans `apps/web/static/web/`). Les vues appellent les mêmes
services que l'API : aucune règle métier n'y est dupliquée.

- **Parcours** : envoi avec devis en direct → connexion par code SMS →
  confirmation (« Payer X HTG ») → paiement plopplop dans un nouvel onglet
  ou demande USSD → page de suivi rafraîchie toutes les 5 s → historique.
- **URL de retour plopplop** (espace marchand) : `https://plip.ht/paiement/retour/`.
  Le format du retour n'est pas documenté : la page accepte notre référence
  (`refference_id`, `reference`…) ou l'identifiant plopplop
  (`transaction_id`…), et à défaut ouvre le dernier transfert en cours du
  client. Elle ne confirme **rien** et ne change aucun état : seul le
  polling de `paiement-verify` fait foi.
- **Contrôle d'admission** : au-delà d'une attente projetée de 4 h pour un
  nouvel entrant (`ADMISSION_MAX_WAIT_SECONDS`), ou si le worker de
  décaissement est à l'arrêt, les **créations sont refusées**. Vérifié à la
  création seulement : un transfert déjà encaissé est une dette et se
  décaisse toujours. Le client est prévenu dès l'accueil, sans jamais
  apprendre la longueur de la file ; l'API répond 503 `SERVICE_SATURATED`
  avec un simple délai de réattente. Chaque refus est audité
  (`admission.refused`). `ADMISSION_CONTROL_ENABLED=0` rouvre tout.
  Un opérateur (transaction sans client) n'est jamais bloqué.
- **Plafonds cumulés par client** : 50 000 HTG sur 24 h glissantes,
  200 000 HTG sur 30 jours glissants, réglables par le superadmin (console →
  Méthodes et tarifs). Vérifiés **à la création uniquement** : une
  transaction encaissée est une dette, aucun plafond ne bloque son
  décaissement. Comptent les transferts encaissés et non remboursés, plus
  ceux en attente de paiement tant que le délai court — sinon dix transferts
  créés puis payés d'un coup passeraient sous le radar. Un remboursement ou
  une expiration libère : la consommation est **calculée, jamais stockée**.
  Chaque refus écrit une ligne d'audit (`limit.refused`) après l'annulation
  de la transaction, sinon elle disparaîtrait avec elle.
- **Notifications SMS** (`apps/notifications`) : un SMS à l'expéditeur
  quand le transfert est livré (avec la référence) et quand il est
  remboursé. Rien au bénéficiaire. Envoi asynchrone : un échec ne bloque ni
  ne fait échouer une transaction, et sans identifiants Twilio aucun appel
  n'est tenté — le déploiement passe, avec l'avertissement `plipplip.W003`.
  Un seul SMS par transaction et par événement : contrainte d'unicité en
  base, plus une clé d'idempotence transmise à Twilio, qui ferme le trou
  entre son acceptation et l'écriture du statut chez nous. Textes sans
  accents, dans la langue de la dernière connexion du client
  (`Customer.language`) — un accent ferait passer le SMS en UCS-2, donc à
  70 caractères par segment et au double du prix.
- **Session client** en cookie `HttpOnly`, distincte de la console : une
  session client n'ouvre pas la console et inversement. Identifiant de
  session renouvelé à la connexion, déconnexion après 30 min d'inactivité
  (`WEB_SESSION_IDLE_SECONDS`).
- **Double clic** : chaque page de confirmation porte sa clé d'idempotence ;
  un double envoi ne crée qu'un transfert.
- **Langues** : français par défaut, créole haïtien (`ht`) et anglais,
  choix en pied de page. Après modification d'un texte :

  ```bash
  python manage.py makemessages -l en -l ht --add-location file --no-obsolete --ignore ".venv/*" --ignore "tests/*"
  # traduire dans apps/web/locale/<langue>/LC_MESSAGES/django.po, puis :
  python manage.py compilemessages --ignore ".venv/*"
  ```

  Les `.mo` compilés sont versionnés. Les emplacements (`--add-location
  file`) sont indispensables : les tests s'en servent pour reconnaître le
  chemin de l'argent. Une chaîne créole non traduite fait échouer les tests,
  sauf si elle est listée dans `apps/web/locale/ht/A_TRADUIRE.md` — ce qui
  est interdit pour les montants, frais, confirmation, erreurs et statut.
  Le catalogue créole existant a été rédigé sans locuteur natif : à relire.
- **Délai affiché** pendant le décaissement : fourchette arrondie vers le
  haut (« moins de 5 minutes », « environ 10 à 15 minutes »), promise une
  fois à l'entrée en file et jamais élargie. Si elle ne tient plus, si le
  float ne couvre pas le transfert, au-delà d'une heure
  (`ETA_MAX_DISPLAY_SECONDS`) ou si le worker de décaissement est à
  l'arrêt, plus aucune durée : « traité dès que possible ». Ni rang ni
  profondeur de file ne sortent vers le client. Rafraîchissement de la page
  de suivi : 5 s, puis 15 s après 1 min, 30 s après 5 min. La vue de file
  servie aux clients est **partagée entre toutes les requêtes**
  (`QUEUE_VIEW_CACHE_SECONDS`) : sans elle, chaque appel de statut relisait
  la file entière. Elle est reconstruite dès que la file bouge — l'empreinte
  est une seule agrégation sur un index, pas une relecture.
- **Worker de décaissement à l'arrêt** (file non vide, aucun retrait tenté
  depuis `PAYOUT_STALL_SECONDS`) : bandeau d'incident sur toutes les pages
  de la console.
- **Production** : fichiers statiques servis par WhiteNoise
  (`python manage.py collectstatic`).

## API publique — `/api/v1/`

Documentation interactive : `/api/v1/docs` (schéma OpenAPI : `/api/v1/schema`).
Désactivée par défaut en production (`API_DOCS_ENABLED=1` pour l'ouvrir).

| Route | Accès | Rôle |
|---|---|---|
| `POST auth/otp/request` · `POST auth/otp/verify` | public | Code SMS, puis jeton `ppk_…` |
| `POST auth/logout` · `GET me` | client | Révocation, profil |
| `GET meta` · `POST quotes` | public | Portefeuilles ouverts, devis |
| `GET/POST transfers` · `GET transfers/{reference}` | client | Historique, création, statut |

- **Clients ≠ opérateurs.** Un `Customer` n'est pas un `User` Django : un
  jeton client n'ouvre ni la console ni l'admin, et une session console
  n'ouvre pas l'API.
- **Idempotence.** `POST transfers` exige `Idempotency-Key`. Une clé
  rejouée rend le transfert déjà créé, sans nouvel appel à plopplop.
- **Devis confirmé.** Le client renvoie `expected_total` ; s'il ne
  correspond plus au devis, refus 409 `QUOTE_CHANGED`.
- **Statuts publics.** Les états internes ne sortent jamais :
  `PAYOUT_UNKNOWN` et `PAYOUT_FAILED` s'affichent « en cours ».
- **Création de paiement indéterminée.** Jamais recréée : la transaction
  passe en attente de paiement et le polling la retrouve par référence.
- **Codes SMS.** `OTP_BACKEND=console` en dev (code écrit dans les logs),
  `twilio` imposé en production ; `check --deploy` refuse le backend console.
- **Méthodes.** Le superadmin ouvre ou ferme chaque portefeuille en entrée
  et en sortie (console → Méthodes). Seules les nouvelles transactions sont
  concernées.

## Trois règles structurantes

**L'état d'une transaction ne change que par `Transaction.transition()`.**
La fonction verrouille la ligne, valide contre la table des transitions
autorisées, et écrit un `TransactionEvent`. Aucune affectation directe de
`state` ailleurs dans le code.

**Un décaissement d'issue inconnue ne se rejoue jamais.**
Timeout, 5xx ou `DUPLICATE_REFERENCE` sur `api/withdraw/marchand` mènent à
`PAYOUT_UNKNOWN`. Cet état n'a aucune transition vers la file : la seule
sortie passe par `api/withdraw/marchand/verify`. C'est la protection
contre le double paiement, et elle est testée.

Un 404 à la vérification ne suffit pas à remettre en file : juste après
un timeout, ce peut être une course écriture/lecture chez plopplop. Il
faut deux 404 pour la même référence, espacés d'au moins
`PAYOUT_VERIFY_GRACE_SECONDS`. Et un 404 sur un retrait que plopplop a
déjà reconnu comme « en attente » (`PAYOUT_PENDING`) ne remet jamais en
file : il se traite à la main.

**Un encaissement non conforme ne part jamais en décaissement.**
Si plopplop confirme un paiement d'un autre montant que le devis — en
plus ou en moins — ou sans communiquer le montant, la transaction reste
en `PAYMENT_CONFIRMED` (`AMOUNT_MISMATCH` / `AMOUNT_UNVERIFIED`) et
remonte sur l'écran exceptions. Le grand livre constate le montant
réellement reçu, entièrement dû au payeur. Sorties : remboursement du
montant reçu, ou déblocage si l'opérateur vérifie chez plopplop que le
montant exact du devis a été encaissé.

**Tout mouvement d'argent produit une écriture équilibrée.**
Le grand livre refuse une écriture dont la somme n'est pas nulle, et
n'accepte ni modification ni suppression. Une erreur se corrige par une
contre-écriture.

## Déploiement Railway

Une seule image (`Dockerfile`, Python 3.13), quatre services qui ne
diffèrent que par leur commande de démarrage, plus Postgres et Redis.

| Service Railway | Config-as-code path | Rôle | Répliques |
|---|---|---|---|
| `web` | `/railway.toml` (défaut) | Site, API, console. Pré-déploiement `manage.py predeploy`, healthcheck `/health/` | 1 ou plus |
| `celery-payouts` | `/railway-celery-payouts.toml` | **Seul** exécutant des retraits plopplop (file `payouts`, concurrence 1) | **1, jamais plus** |
| `celery-worker` | `/railway-celery.toml` | Polling des paiements, vérifications, float (file `celery`) | 1 ou plus |
| `celery-beat` | `/railway-beat.toml` | Planificateur | **1, jamais plus** |
| `Postgres` | — | Base (service Railway) | — |
| `Redis` | — | Broker Celery, cache, verrou des retraits (service Railway) | — |

Mise en place :

1. Créer le projet, ajouter **Postgres** et **Redis**.
2. Créer les 4 services depuis le dépôt GitHub. Pour chacun sauf `web` :
   *Settings → Config-as-code → path* vers son fichier `.toml`.
3. Renseigner les variables (ci-dessous). Les variables marquées « tous »
   doivent être présentes sur les 4 services : les workers chargent les
   mêmes réglages de production et refusent de démarrer sans elles.
4. Générer un domaine public sur `web` uniquement.
5. Renseigner `SUPERADMIN_PASSWORD` sur `web` : le compte superadmin de la
   console (`info@plip.ht` par défaut) est créé au déploiement. Se connecter
   sur `/console/login/` et valider la tarification.

`manage.py predeploy` (avant chaque mise en service de `web`) exécute
`check --deploy`, les migrations, `init_ledger` et `init_superadmin` ; s'il
échoue, l'ancienne version reste en ligne.

**Superadmin** (`init_superadmin`) : `SUPERADMIN_USERNAME` (défaut : l'email),
`SUPERADMIN_EMAIL` (défaut `info@plip.ht`), `SUPERADMIN_PASSWORD`. Les
variables font foi : le compte est créé s'il n'existe pas, remis en
superadmin actif, et son mot de passe aligné sur la variable. Changer le
mot de passe dans Railway redéploie `web` et l'applique. Un mot de passe vide
ou refusé (moins de 12 caractères, trop courant, entièrement numérique,
proche de l'identifiant) ne crée ni ne modifie rien et n'empêche pas le
déploiement : le motif est dans les logs. Chaque changement est inscrit au
journal d'audit, sans le mot de passe.

### Variables d'environnement

| Variable | Services | Valeur |
|---|---|---|
| `DJANGO_SECRET_KEY` | tous | Chaîne aléatoire d'au moins 50 caractères |
| `DATABASE_URL` | tous | `${{Postgres.DATABASE_URL}}` |
| `REDIS_URL` | tous | `${{Redis.REDIS_URL}}` |
| `PLOPPLOP_CLIENT_ID` · `PLOPPLOP_CLIENT_SECRET` | tous | Identifiants marchand plopplop |
| `TWILIO_ACCOUNT_SID` · `TWILIO_AUTH_TOKEN` · `TWILIO_VERIFY_SERVICE_SID` | tous | Compte et service Twilio Verify |
| `DJANGO_ALLOWED_HOSTS` | web | Domaines personnalisés, séparés par des virgules (le domaine `*.up.railway.app` est ajouté automatiquement) |
| `CSRF_TRUSTED_ORIGINS` | web | Facultatif : origines `https://…` supplémentaires |
| `SUPERADMIN_USERNAME` · `SUPERADMIN_EMAIL` · `SUPERADMIN_PASSWORD` | web | Compte superadmin créé ou mis à jour au déploiement (voir ci-dessus) |
| `API_NUM_PROXIES` | web | `1` (proxy Railway devant l'application ; sans cela, les limites par IP se basent sur l'adresse du proxy) |
| `DATABASE_SSL_REQUIRE` | tous | Facultatif, `1` pour exiger TLS vers Postgres |

Réglages facultatifs, valeurs par défaut dans `.env.example` :
`PAYOUT_COOLDOWN_SECONDS`, `PAYOUT_MAX_ATTEMPTS`, `PAYMENT_EXPIRY_SECONDS`,
`PAYOUT_PENDING_STALE_SECONDS`, `PAYOUT_VERIFY_GRACE_SECONDS`,
`MAX_NET_AMOUNT`, `FLOAT_WARNING`, `FLOAT_CRITICAL`,
`API_TOKEN_TTL_SECONDS`, `WEB_SESSION_IDLE_SECONDS`, `API_DOCS_ENABLED`.

`DJANGO_SETTINGS_MODULE=config.settings.prod` est fixé dans l'image.

### Points de vigilance

- **Config as Code est déprécié par Railway** : les fichiers `railway*.toml`
  fonctionnent jusqu'au **1er décembre 2026**. Migrer avant vers
  l'Infrastructure as Code (`railway config migrate`).
- **IP de sortie.** Le cooldown plopplop est compté par IP. Si plopplop
  doit mettre notre IP en liste blanche, il faut une IP sortante fixe
  (option Railway « Static Outbound IPs ») sur `celery-payouts`, et sur
  `web` / `celery-worker` si plopplop filtre aussi l'encaissement.
- **Pas d'environnement de test chez plopplop** : le premier déploiement
  parle à la production plopplop. Recette avec de petits montants.
- **Sauvegardes** : les sauvegardes Railway vivent chez Railway et ne
  protègent pas de la perte de Railway. Un export hebdomadaire
  (`manage.py export_data --encrypt`) doit vivre ailleurs. Marche à suivre
  et restauration : [RESTAURATION.md](RESTAURATION.md).

## La contrainte du cooldown

plopplop impose **120 secondes entre deux retraits, par IP**. La limite
est donc globale à la plateforme, pas par utilisateur. Le décaissement
doit tourner sur un worker unique :

```bash
celery -A config worker -Q payouts --concurrency=1 -n payouts@%h
celery -A config worker -Q celery --concurrency=4 -n general@%h
celery -A config beat
```

Un verrou Redis double la protection, au cas où deux processus de
décaissement tourneraient en même temps — double démarrage par erreur, ou
recouvrement de l'ancienne et de la nouvelle instance pendant un
déploiement. Sa durée de vie est courte (`PAYOUT_LOCK_TTL_SECONDS`, 240 s
par défaut) et il est rafraîchi avant chaque retrait ; un worker tué ne
bloque la file que jusqu'à son expiration. Si le rafraîchissement échoue,
le lot s'arrête avant le retrait suivant. Les déclenchements de beat
expirent après un intervalle (`PAYOUT_DRAIN_INTERVAL_SECONDS`) pour ne pas
s'empiler.

**Aucune tâche ne dort.** Quand le cooldown n'est pas écoulé, la tâche rend
la main et beat la relance : au pire 30 s de latence. Un processus qui dort
dix minutes finit tué au milieu d'un retrait — c'est ce qui fabriquait les
décaissements orphelins à chaque déploiement.

**Décaissement orphelin.** Une transaction restée « en cours » au-delà de
`PAYOUT_INFLIGHT_STALE_SECONDS` (600 s) n'a plus de processus derrière elle.
`sweep_stale_payouts` la reprend en `PAYOUT_UNKNOWN` — **jamais vers la
file** : l'argent est peut-être parti, la vérification auprès de l'opérateur
tranche. L'âge est lu dans le journal d'événements, pas dans `updated_at`.
Si des orphelins s'accumulent, la console les signale en rouge sur l'écran
Exceptions : c'est que le balayage ne tourne plus.

Débit maximal qui en résulte : **environ 30 décaissements par heure.**

C'est vivable pour le MVP B2C. Ce ne l'est pas pour le module Payroll :
un lot de 200 employés demanderait près de 7 heures. Le Payroll ne peut
pas être construit sur ce dispositif tant que le plafond n'a pas été
relevé.

## Tarification

Réglée par le **superadmin** dans la console (écran « Méthodes et tarifs »),
jamais dans `.env`. Tant qu'elle n'a pas été validée, le tableau de bord
le lui signale dès la connexion.

- **Frais client** par portefeuille : à l'envoi (portefeuille source) et à
  la réception (destination), plus la **commission Plip-Plip**. Base : le
  montant reçu par le bénéficiaire.
- **Coûts plopplop** par portefeuille : à l'encaissement et au retrait.
  Ils servent aux estimations (grand livre, couverture de la file, marge) ;
  le `fee` réel retourné par plopplop prime dès qu'il est connu.
- Un devis et ses coûts estimés sont **figés** sur la transaction : changer
  les taux ne touche que les nouveaux devis.
- Une route ouverte à marge estimée négative est signalée à tous sur le
  tableau de bord.

Tarifs de départ : frais client 3 % par côté et commission 3 % (note
conceptuelle), retrait plopplop MonCash 4 % et NatCash 2,5 % (plopplop),
coût d'encaissement inconnu (0 %).

**Les encaissements créditent le float.** Doc plopplop : « Les paiements
clients créditent votre solde marchand (prépayé) ». Chaque transfert
finance donc son propre décaissement ; le float ne sert que de tampon.

## Questions ouvertes à poser à plopplop

Réponses de la documentation v1.6 (`/paiement-doc`) : les paiements
créditent le solde prépayé ; `paiement-verify` renvoie `montant` ; les
frais de retrait viennent de la configuration des moyens de paiement
(2,5 % NatCash dans l'exemple) ; aucun environnement de test ; aucun
webhook. Restent :

1. **Que retient plopplop sur un encaissement ?** Non documenté. Réglé à
   0 % en attendant ; le drift des relevés de float révélera l'écart.
2. **Le cooldown de 120 s peut-il être relevé ou notre IP mise en liste
   blanche ?** Détermine si le Payroll est réalisable.
3. **Une référence est-elle réutilisable après un retrait échoué ?** Non
   documenté. Chaque tentative porte un suffixe `-W1`, `-W2`… et on ne
   réutilise jamais.
4. **Carte : dans quelle devise `paiement-verify` renvoie-t-il `montant` ?**
   La doc indique un débit en USD après conversion. S'il renvoie des USD,
   chaque paiement carte sera bloqué en « montant non conforme ».

Pas d'environnement de test : la recette se fait en production, sur de
petits montants (20 HTG minimum).

## Écarts assumés avec la note conceptuelle

**Un seul float, pas deux.** La note prévoit un `Float MonCash` et un
`Float NatCash` à rééquilibrer. plopplop expose un solde prépayé unique
(`balance_after`), utilisable vers les deux réseaux. Le Liquidity
Management Engine se réduit à une surveillance de seuil — le
rééquilibrage inter-réseaux n'existe plus.

**Deux seuils de trésorerie, pas un.** Le seuil absolu (le float passe
sous un plancher) et la couverture (le float ne couvre plus les
engagements en cours). Le second est le vrai signal de risque et
n'apparaît pas dans la note.

**Plus de moyens en entrée qu'en sortie.** plopplop accepte aussi
Kashpaw et carte à l'encaissement, mais ne décaisse que vers MonCash et
NatCash. `PAYOUT_CAPABLE` encode cette asymétrie. Carte → NatCash est
donc possible dès le MVP, ce que la note n'envisageait pas.

## À construire ensuite

- Front web et app Flutter sur l'API v1
- Désactivation d'un client depuis la console
- Notifications SMS/e-mail de suivi des transferts
- Réconciliation : comparaison `FloatSnapshot.provider_balance` vs solde
  calculé au grand livre
