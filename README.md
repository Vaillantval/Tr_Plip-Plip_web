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
