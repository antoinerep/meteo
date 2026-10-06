# Faire tourner la collecte toute seule sur GitHub

Le principe : GitHub exécute le script selon un calendrier, puis **commite les
données dans le dépôt lui-même**. Pas de base de données, pas de serveur, pas
d'abonnement. Le dépôt *est* la base de données, et chaque relevé est horodaté
par un commit.

Tout est déjà en place dans ce dépôt. Ce document décrit ce qu'il faut régler
une fois, et les pièges de ce montage.

---

## Les deux réglages nécessaires

### 1. Autoriser le workflow à écrire

C'est **l'étape qu'on oublie**, et sans elle le workflow tourne mais échoue au
moment de pousser les données, avec un `403` peu parlant.

**Settings → Actions → General**, tout en bas, section *Workflow permissions* :
cocher **Read and write permissions**, puis **Save**.

### 2. Déclencher une première exécution

Sans attendre le lendemain matin : **Actions → Collecte météo → Run workflow**,
en laissant le champ *backfill* vide. Deux à trois minutes. Si le job est vert,
un commit `meteo: collecte …` signé *github-actions[bot]* doit être apparu.

Pour constituer l'historique depuis zéro, relancer une fois avec
`backfill = 2470` : environ sept ans d'observations horaires.

---

## Ce qui tourne ensuite

| Quand (UTC) | Pourquoi cette heure |
|---|---|
| 06 h 40 | Météo-France publie ses fichiers horaires vers 05 h 45 ; on laisse une marge |
| 18 h 40 | rafraîchit la prévision avec le run du soir |

Les minutes sont volontairement décalées de l'heure ronde : GitHub exécute
beaucoup de tâches à `:00` et saute volontiers celles qui s'y présentent.

---

## Les trois pièges

### Les planifications s'arrêtent après 60 jours d'inactivité

GitHub désactive les workflows planifiés d'un dépôt resté **60 jours sans
activité humaine**. Les commits du robot ne comptent pas toujours. Un e-mail
prévient avant, avec un bouton pour réactiver.

Pour ne jamais y penser, une tâche gratuite sur <https://cron-job.org> appelant
une fois par jour :

```
POST https://api.github.com/repos/<compte>/meteo/dispatches
Authorization: Bearer <jeton personnel>
Accept: application/vnd.github+json
Corps : {"event_type":"collect"}
```

Le workflow écoute déjà cet événement (`repository_dispatch: types: [collect]`).

### Les planifications sont approximatives

Un `cron` GitHub n'est pas une garantie : des retards de dix à trente minutes
sont courants, et des exécutions sont parfois purement sautées. Sans gravité
ici — le collecteur relit systématiquement les dix derniers jours, donc une
exécution manquée est rattrapée par la suivante, sans trou dans les données.

### On ne sait pas que ça s'est arrêté

Un workflow qui échoue envoie un e-mail. Un workflow qui **ne se lance plus du
tout** n'envoie rien. C'est le défaut classique de ce montage, et la raison
d'être du battement.

Créer un compte gratuit sur <https://healthchecks.io>, un check de période
12 heures avec une heure de marge, copier son URL de ping, puis
**Settings → Secrets and variables → Actions → New repository secret**, nom
`HEALTHCHECKS_URL`. Le workflow le détecte seul et le pingue à chaque passage.

> **Attention à la portée du secret.** Il doit être déclaré au niveau du *job*,
> pas dans le `env:` de l'étape. La condition `if:` d'une étape est évaluée
> **avant** que son propre `env:` soit appliqué, et le contexte `secrets` n'y
> est pas accessible : déclaré dans l'étape, le battement n'est jamais envoyé,
> et l'étape est simplement sautée sans la moindre erreur. C'est ce dépôt-ci
> qui a servi à diagnostiquer le problème ; le bloc `env:` est bien sous
> `jobs.collect:`.

---

## Vérifier que ça vit

Une fois par mois, trois signes suffisent :

- la page **Actions** montre des pastilles vertes régulières ;
- `ETAT.md`, à la racine, s'affiche directement sur GitHub et porte sa date de
  génération en première ligne ;
- `data/obs/` contient un fichier par mois, qui grossit.

---

## Consommer ces données ailleurs

Le dépôt étant public, tout projet tiers peut le récupérer dans son propre
workflow sans aucun jeton :

```yaml
- uses: actions/checkout@v5
  with:
    repository: <compte>/meteo
    path: meteo
```

Les deux seuls fichiers à connaître sont `data/daily.json` et
`data/forecast_daily.json` — leur forme est documentée dans le README et ne
changera pas.
