# Météo locale — bassin de Monistrol-sur-Loire

Collecte en continu la météo d'un rayon de 22 km autour de Monistrol-sur-Loire
(Haute-Loire) : pluie et température réellement mesurées par les stations
Météo-France, plus l'analyse et la prévision haute résolution sur treize points
précis du relief.

Ce dépôt ne fait **que** de la météo. Il expose des séries journalières
propres, documentées et stables ; ce qu'on en fait ensuite ne le regarde pas.
C'est ce qui le rend réutilisable tel quel.

## Ce qu'on récupère, et pourquoi c'est bon

| Source | Résolution | Profondeur | Clé |
|---|---|---|---|
| Stations Météo-France (`BASE/HOR` + `HOR_COMP` sur data.gouv.fr) | 14 stations horaires, pluie + température | depuis 1920, maj quotidienne ~05 h 45 UTC | non |
| Open-Meteo / Météo-France AROME France HD | 1,3 km, horaire | archive depuis 2024, prévision J+5 | non |
| Open-Meteo / ARPEGE + meilleur modèle | 11 km | prévision J+14, variables de sol | non |

Le réseau de stations est inhabituellement dense ici, et surtout il couvre tout
l'étagement :

| Station | Altitude | Distance |
|---|--:|--:|
| Bas-en-Basset | 446 m | 4,5 km |
| Beauzac | 643 m | 4,7 km |
| **Monistrol-sur-Loire** | 777 m | 5,2 km |
| Aurec-sur-Loire | 820 m | 10,7 km |
| St-Romain-Lachalm | 902 m | 12,6 km |
| …et neuf autres jusqu'à 22 km | 535–960 m | |

Cet étagement n'est pas un détail. Sur les 90 derniers jours, St-Romain-Lachalm
(902 m) a reçu **64 % de pluie de plus** que Bas-en-Basset (446 m), à treize
kilomètres de distance. Une seule station ne décrit pas ce territoire.

## Démarrer

Rien à installer : bibliothèque standard Python 3.11+ uniquement.

```bash
python3 collect.py --refresh-stations      # découvre les stations du rayon
python3 collect.py --backfill 2470         # ~6,7 ans d'observations
python3 collect.py --only grid --backfill 1015   # archive AROME HD depuis 2024
python3 consolidate.py                     # fabrique daily.json
python3 summary.py                         # écrit ETAT.md
```

Ensuite, en routine, `python3 collect.py` suffit : il reprend les dix derniers
jours d'observations (Météo-France corrige parfois a posteriori), le dernier
run de prévision, et les sept derniers jours d'archive.

## Les trois flux

```
collect.py --only obs        data/obs/AAAA-MM.jsonl.gz
                             observations horaires des stations, telles que
                             mesurées. La vérité terrain.

collect.py --only forecast   data/forecast/AAAA-MM-JJ.jsonl.gz
                             un enregistrement par (run, point, modèle,
                             échéance). Le run_ts est conservé : on pourra
                             mesurer plus tard la qualité des prévisions.

collect.py --only grid       data/grid/AAAA-MM.jsonl.gz
                             archive horaire par point. L'air vient d'AROME HD
                             (1,3 km), le sol de best_match, seul à sortir
                             l'évapotranspiration et l'humidité du sol.
```

Les relances sont idempotentes : une ligne déjà présente est remplacée, jamais
dupliquée, et le gzip est écrit à `mtime=0` pour que git ne voie un diff que
si le contenu a vraiment changé.

## Le contrat de sortie

`consolidate.py` produit les deux seuls fichiers que les consommateurs doivent
connaître. Quoi qu'il arrive aux formats bruts, ces deux-là gardent leur forme.

**`data/daily.json`** — passé, par source et par jour :

```json
{
  "sources": {
    "station:43137003": {"kind":"station","name":"MONISTROL-SUR-LOIRE",
                         "lat":45.314,"lon":4.231,"alt":777,"dist_km":5.2},
    "point:orcimont":   {"kind":"point","name":"Bois d'Orcimont",
                         "lat":45.30,"lon":4.205,"alt":760}
  },
  "daily": {
    "point:orcimont": {
      "2026-10-03": {"rr":4.6,"tmin":12.3,"tmax":20.9,"tmoy":16.3,
                     "rh":78,"et0":1.9,"tsol":14.8,"sm":0.201,"hours":24}
    }
  }
}
```

**`data/forecast_daily.json`** — prévision du dernier run, par point et par
jour, avec pour chaque grandeur le modèle dont elle provient :

```json
{"run_ts":"2026-10-04T20:22:43+00:00",
 "points":{"orcimont":{"2026-10-07":{"rr":7.1,"tmax":19.4,"tsol":13.9,
   "models":{"rr":"meteofrance_arome_france_hd","tsol":"best_match"}}}}}
```

Chaque grandeur est prise au modèle le plus fin qui la fournit ce jour-là :
AROME HD tant qu'il porte, puis ARPEGE, puis le modèle global ; les variables
de sol viennent toujours de `best_match`, seul à les calculer. D'où le champ
`models` : sans lui, on ne saurait pas qu'une même colonne change de résolution
en cours de route. Il permet par exemple de n'appliquer une correction de biais
qu'aux échéances réellement issues du modèle sur lequel elle a été mesurée.

Une journée météo court de 06 h à 06 h UTC, comme les cumuls Météo-France : une
pluie de nuit est attribuée à la veille, ce qui correspond à la façon dont on
en parle.

## Automatisation

`.github/workflows/collect.yml` tourne deux fois par jour et commite les
données :

- **06 h 40 UTC**, juste après la publication des fichiers Météo-France ;
- **18 h 40 UTC**, pour rafraîchir la prévision avec le run du soir.

La surveillance est en trois couches, parce qu'aucune ne suffit seule : échec
du workflow signalé par e-mail GitHub ; détection d'anomalie dans `collect.py`,
qui sort en erreur si un run n'obtient aucune observation pendant les heures
ouvrées ; et un battement healthchecks.io optionnel, activé dès que le secret
`HEALTHCHECKS_URL` est renseigné. Seul le troisième détecte que GitHub Actions
lui-même n'a pas tourné — les deux autres supposent que le job démarre.

Les horaires sont décalés de 40 minutes : les planifications GitHub Actions
sont au mieux approximatives et souvent ignorées en haut d'heure.

## Volumétrie

Environ 15 Mo pour sept ans d'observations horaires et deux ans d'archive
AROME HD sur treize points, soit à peu près 1 Mo par mois de données nouvelles.
Rien qui pose problème à un dépôt git.

## Ajouter un point

Dans `config.json`, un objet de plus dans `points` : `id`, `name`, `lat`,
`lon`, `alt`. Puis `python3 collect.py --only grid --backfill 1015` pour lui
constituer un historique. Aucun code à toucher.

## Ce que ce projet ne fera pas

Interpréter. Pas de seuil « il fait beau », pas de score, pas d'alerte métier.
Si une question commence par « est-ce que c'est bien pour… », elle appartient à
un projet consommateur, pas à celui-ci.
