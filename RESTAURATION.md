# Restaurer Plip-Plip

**Ce document se lit à 2 h du matin.** Chaque commande se copie telle
quelle. Si une étape vous oblige à deviner quelque chose, elle est fausse :
signalez-la, elle sera réécrite.

Vous aurez besoin de trois choses :

1. le fichier de sauvegarde `plipplip-AAAAMMJJ-HHMMSS.json` (ou `.json.enc`) ;
2. le fichier `…-operateurs.json.enc` qui l'accompagne ;
3. la phrase secrète des sauvegardes.

---

## 0. Avant tout : ne rien supprimer

Ne supprimez pas le service, ne supprimez pas le volume, ne relancez pas
une migration. Une base cassée reste une source d'information ; une base
supprimée n'en est plus une.

Constatez d'abord, dans cet ordre :

```bash
railway status                  # le service répond-il ?
railway logs --service web      # que dit-il exactement ?
```

Puis demandez-vous **ce qui a été perdu** :

| Symptôme | Où aller |
|---|---|
| L'application ne démarre pas, la base est intacte | Ce n'est pas une restauration. Corrigez le déploiement. |
| La base répond mais les chiffres sont faux | Étape 1, puis 4. **Ne restaurez pas encore.** |
| La base est vide, corrompue ou perdue | Étape 1. |

---

## 1. Vérifier la sauvegarde **avant** de toucher à quoi que ce soit

Cette étape ne modifie rien. Elle répond à une seule question : *ce fichier
est-il restaurable ?*

```bash
export PLIPPLIP_BACKUP_PASSPHRASE='la phrase secrète'
python manage.py verify_data plipplip-20260917-200158.json.enc
```

Sortie attendue — cinq lignes `[  OK  ]` :

```
[  OK  ] Format du fichier : version 1
[  OK  ] En-tete : 13 tables, 13 lignes, comptes conformes
[  OK  ] Equilibre de chaque ecriture : 0 ecritures
[  OK  ] Somme globale des lignes : 0 lignes, somme nulle
[  OK  ] Liens internes du fichier : 9 cles etrangeres verifiees
```

**Un seul `[ ECHEC]` : arrêtez-vous.** Prenez la sauvegarde précédente et
recommencez cette étape. Ne restaurez jamais un fichier qui échoue ici.

### Si vous voulez lire le fichier

Il est en JSON, volontairement lisible : quand on restaure, c'est qu'il y a
déjà un problème, et pouvoir ouvrir le fichier et regarder est précieux.

```bash
openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
  -in plipplip-20260917-200158.json.enc \
  -out lisible.json \
  -pass env:PLIPPLIP_BACKUP_PASSPHRASE
```

Cette commande n'a besoin **ni de Python, ni de Django, ni de ce dépôt** :
c'est pour cela qu'elle a été choisie. Le jour où vous restaurez,
l'application est peut-être justement ce qui ne démarre pas.

Supprimez `lisible.json` quand vous avez fini. Il est en clair.

---

## 2. Se brancher sur la bonne base

**L'export et la restauration ne dépendent pas de Railway.** Ce sont des
fichiers et une commande Django ; ils tournent depuis votre machine.

Pour viser la base Railway depuis chez vous, copiez les valeurs de l'onglet
**Variables** du service Postgres — celles du proxy public, pas celles du
réseau interne :

```bash
export DB_HOST=<RAILWAY_TCP_PROXY_DOMAIN>
export DB_PORT=<RAILWAY_TCP_PROXY_PORT>
export DB_NAME=<PGDATABASE>
export DB_USER=<PGUSER>
export DB_PASSWORD=<PGPASSWORD>
export DJANGO_SETTINGS_MODULE=config.settings.prod
```

Pour restaurer sur une base locale à la place, utilisez
`--settings=config.settings.dev` : rien d'autre ne change.

**Vérifiez sur quelle base vous êtes avant de continuer :**

```bash
python manage.py verify_data
```

La ligne « Soldes par compte » vous dit immédiatement si vous êtes sur la
bonne. Si elle affiche des montants inattendus, vous n'êtes pas où vous
croyez.

---

## 3. Restaurer

Créez d'abord les tables si la base est neuve :

```bash
python manage.py migrate
```

Puis :

```bash
python manage.py import_data \
  plipplip-20260917-200158.json.enc \
  --operateurs plipplip-20260917-200158-operateurs.json.enc \
  --force
```

- `--force` **vide la base** avant de restaurer. Sans ce drapeau, une base
  non vide est refusée et rien n'est touché — c'est volontaire.
- Sans `--operateurs`, les comptes opérateurs ne sont pas restaurés et les
  traces « qui a fait quoi » perdent leur auteur. **L'argent, lui, est
  complet.** Vous pourrez toujours vous connecter avec le superadmin recréé
  par `init_superadmin`.
- La commande vérifie le fichier avant d'écrire, et vérifie la base après.
  Si elle s'arrête sur `NE PAS remettre en service`, n'ouvrez pas le site.

---

## 4. Vérifier

```bash
python manage.py verify_data
```

Les sept contrôles doivent passer. Lisez surtout :

- **« Equilibre de chaque ecriture »** — la comptabilité tient.
- **« Dette clients au grand livre »** — ce que nous devons aux clients
  correspond aux transferts en cours. Un écart ici veut dire que de
  l'argent encaissé n'est réclamé par personne, ou l'inverse.
- **« Soldes par compte »** — comparez `float.plopplop` avec le solde réel
  affiché dans l'espace marchand plopplop. **C'est la seule vérification
  qui sorte de nos propres livres.**
- **« Derive du float »** — écart avec le dernier relevé externe connu.

---

## 5. Changer les mots de passe opérateurs

**À faire maintenant, pas demain.** Un export restauré est un export qui a
circulé : il est passé par une machine, peut-être par une clé USB. Les mots
de passe qu'il contient sont hachés, donc inexploitables tels quels — mais
un fichier volé se casse hors ligne, tranquillement, sur des comptes qui
peuvent rembourser et recharger le float.

```bash
python manage.py changepassword <identifiant>
```

Un par compte, pour tout le monde.

---

## 6. Prévenir les clients

Les jetons de session ne sont **jamais** sauvegardés. Après restauration,
tous les clients sont déconnectés et se reconnectent par SMS.

**C'est normal, ce n'est pas une panne.** Dites-le au support avant qu'il ne
reçoive les appels.

---

## Où ce fichier ne va jamais

Un export contient **les numéros de téléphone et les montants de tous les
clients**. Ce sont des données personnelles.

| Interdit | Pourquoi |
|---|---|
| E-mail non chiffré | Le message reste sur trois serveurs que nous ne contrôlons pas. |
| WhatsApp, Slack, Telegram | Sauvegardé dans le nuage, indexé, impossible à rappeler. |
| Google Drive partagé, Dropbox public | Un lien « toute personne disposant du lien » est un lien public. |
| Dépôt git, même privé | Un fichier commité reste dans l'historique après suppression. `.gitignore` l'en empêche déjà — ne le contournez pas. |
| Un serveur de fichiers ouvert à l'équipe | Le fichier n'est utile qu'à deux personnes. |

**Autorisé** : un disque chiffré que vous détenez, une clé USB chiffrée
rangée ailleurs qu'au bureau, un stockage privé dont vous seul avez la clé.

Pour déplacer le fichier principal, chiffrez-le d'abord — soit à l'export
avec `--encrypt`, soit après coup :

```bash
openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt \
  -in plipplip-20260917-200158.json \
  -out plipplip-20260917-200158.json.enc \
  -pass env:PLIPPLIP_BACKUP_PASSPHRASE
```

Le fichier `…-operateurs.json.enc` est **toujours** chiffré, sans exception.

**La phrase secrète ne voyage jamais avec le fichier.** Un fichier chiffré
et sa phrase dans le même message, c'est un fichier en clair.

---

## Pour Val — à faire une fois, dans le tableau de bord

Ces quatre points ne se font pas depuis le code. Ils sont à vous.

### 1. Activer les sauvegardes Postgres

Service **Postgres** → onglet **Backups** → choisir les rythmes. Chacun a sa
propre rétention :

| Rythme | Fréquence | Conservation |
|---|---|---|
| Daily | toutes les 24 h | 6 jours |
| Weekly | tous les 7 jours | 1 mois |
| Monthly | tous les 30 jours | 3 mois |

**Activez au moins Daily et Weekly.** Daily seul laisse une plateforme
d'argent avec six jours de mémoire.

Pour restaurer côté Railway : onglet **Backups**, repérer la sauvegarde par
sa date, **Restore**. Railway ne l'applique pas tout de suite — il prépare
un changement, qu'il faut relire puis **Deploy**. Le service redémarre sur
les données restaurées.

### 2. Envisager le PITR

Même onglet **Backups** : bandeau « Point-in-time recovery is off », bouton
**Enable PITR**. Postgres archive alors chaque segment WAL, avec une
sauvegarde complète par semaine et une différentielle par jour ; les quatre
dernières complètes sont conservées, soit environ quatre semaines de
fenêtre.

Ce que cela change : on restaure **à un instant précis**, pas à la
sauvegarde de la veille. Sur une plateforme où chaque heure contient des
transferts, la différence se compte en argent réel. Cela consomme un
stockage facturé : à arbitrer, mais à arbitrer consciemment.

### 3. Le second exemplaire vit ailleurs que chez Railway

C'est le point le plus important de cette page.

Les sauvegardes Railway et le PITR vivent **chez Railway**. Ils protègent
d'une base corrompue, d'une suppression accidentelle, d'un mauvais
déploiement. Ils ne protègent pas de la perte de Railway lui-même :
compte suspendu, litige de paiement, panne prolongée, fermeture.

D'où `export_data`. Le fichier qu'il produit doit finir **hors de Railway**.

### 4. Le rythme

| Quand | Quoi |
|---|---|
| Une fois par semaine | `python manage.py export_data --encrypt`, fichier rangé hors de Railway |
| Avant chaque déploiement qui touche aux modèles ou aux migrations | un export, gardé jusqu'à ce que le déploiement soit confirmé sain |
| Une fois par trimestre | **suivre ce document en entier**, sur une base locale, pour vérifier qu'il marche encore |

Ce dernier point n'est pas décoratif. Une sauvegarde jamais restaurée n'est
pas une sauvegarde : c'est un fichier dont on espère quelque chose.
