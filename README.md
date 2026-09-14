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

## Structure

| Dossier | Rôle |
|---|---|
| `apps/providers/plopplop` | Seul module qui parle à plopplop. Signature HMAC, 3 étapes de retrait, exceptions typées. |
| `apps/transactions` | Machine à états, tarification, orchestration, tâches Celery. |
| `apps/ledger` | Grand livre en partie double, append-only. |
| `apps/treasury` | Suivi du float, seuils, projection de rupture. |
| `apps/accounts` | Utilisateurs, rôles d'exploitation, journal d'audit. |
| `apps/console` | Dashboard superadmin (Django templates + HTMX). |
| `apps/api` | API publique v1 (DRF) pour le front web et l'app Flutter. |
| `apps/providers/twilio` | Seul module qui parle à Twilio Verify (codes SMS). |

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

## Déploiement — la contrainte du cooldown

plopplop impose **120 secondes entre deux retraits, par IP**. La limite
est donc globale à la plateforme, pas par utilisateur. Le décaissement
doit tourner sur un worker unique :

```bash
celery -A config worker -Q payouts --concurrency=1 -n payouts@%h
celery -A config worker -Q celery --concurrency=4 -n general@%h
celery -A config beat
```

Un verrou Redis double la protection, au cas où deux workers `payouts`
seraient démarrés par erreur.

Débit maximal qui en résulte : **environ 30 décaissements par heure.**

C'est vivable pour le MVP B2C. Ce ne l'est pas pour le module Payroll :
un lot de 200 employés demanderait près de 7 heures. Le Payroll ne peut
pas être construit sur ce dispositif tant que le plafond n'a pas été
relevé.

## Questions ouvertes à poser à plopplop

Ces quatre points bloquent des décisions qu'on ne peut pas prendre seuls.

1. **Que retient plopplop sur un encaissement ?** Non documenté. Sans ce
   chiffre, le 3 + 3 + 3 = 9 % de la note conceptuelle est une hypothèse
   et la marge affichée par le dashboard reste une estimation.
2. **Quel est le barème réel du champ `fee` au retrait ?** L'exemple de
   la doc donne 2,5 %, à confirmer et à ventiler par méthode.
3. **Le cooldown de 120 s peut-il être relevé ou notre IP mise en liste
   blanche ?** Détermine si le Payroll est réalisable.
4. **Une référence est-elle réutilisable après un retrait échoué ?** La
   doc ne décrit `DUPLICATE_REFERENCE` que pour les retraits réussis. En
   attendant, chaque tentative porte un suffixe `-W1`, `-W2`… et on ne
   réutilise jamais.

Point mineur à faire trancher aussi : la doc se contredit sur la durée du
jeton d'authentification — texte « ~1 minute », champ `expires_in: 300`.
Le code ne met aucun jeton en cache.

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
