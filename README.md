# Boucles vélo pré-calculées : guide pas à pas (POC à 0 €)

Ce projet génère un catalogue de boucles vélo autour de villes de départ (région de test : **province de
Barcelone**), avec distance, dénivelé, % de zone bâtie, % de pistes cyclables… et un « pitch » qui explique
les choix. Tout est calculé **chez GitHub, gratuitement** : rien à installer sur ton ordinateur.

---

## 1. Ce qu'il te faut

**Un seul compte obligatoire : GitHub** (gratuit).

**Logiciels : aucun à installer.**
- Un navigateur récent (Chrome, Edge ou Firefox).
- L'extraction de fichiers zip intégrée à Windows (clic droit → « Extraire tout »).

**Ne les installe pas maintenant** (inutiles pour cette étape) : Java, Python, Git, Docker, osmium, VS Code, Claude Code.

**Aucun compte requis pour les données** : OpenStreetMap via Geofabrik, tuiles d'altitude AWS, OpenFreeMap et Open-Meteo
(gratuit pour un usage non commercial).

**Facultatif** : un compte OpenStreetMap (gratuit) si tu veux corriger sur la carte des erreurs que tu repères en
jugeant les boucles. **Plus tard** : la web app sera hébergée sur GitHub Pages, avec le même compte.

---

## 2. Pas à pas (à faire sur ordinateur, pas sur téléphone)

### Étape 1 : créer ton compte GitHub (5 min)
1. Va sur github.com → **Sign up**.
2. Renseigne e-mail, mot de passe et **nom d'utilisateur**. Il sera public et fera partie de l'adresse de ton futur
   site (`nom.github.io`) : choisis-en un que tu garderas.
3. Confirme ton adresse e-mail.
4. Recommandé : active la double authentification (Settings → Password and authentication).

### Étape 2 : préparer les fichiers
1. Télécharge `velo-loops.zip`.
2. Clic droit → **Extraire tout**.
3. Ouvre le dossier extrait. Tu dois voir **directement** : `.github`, `config`, `scripts`, `web`, `README.md`, `.gitignore`.

### Étape 3 : créer le dépôt (l'espace de stockage du projet)
1. Sur GitHub : bouton **+** en haut à droite → **New repository**.
2. *Repository name* : `velo-loops`.
3. Choisis **Public** (obligatoire pour que les calculs soient gratuits). Ne coche aucune autre case.
4. **Create repository**, puis clique sur le lien **« uploading an existing file »**.

### Étape 4 : envoyer les fichiers
1. Dans le dossier extrait, sélectionne tout (**Ctrl + A**).
2. **Glisse le contenu** (pas le dossier `velo-loops` lui-même) dans la zone de la page GitHub.
3. Attends la fin de l'envoi et vérifie que la liste contient `.github/workflows/build-loops.yml`.
4. Bouton vert **Commit changes**.
5. Vérifie : à la racine du dépôt tu dois voir `.github`, `config`, `scripts`, `web`. Si tu vois un seul dossier
   `velo-loops`, c'est le mauvais envoi : supprime le dépôt (Settings → tout en bas « Delete this repository »)
   et recommence les étapes 3 et 4.

### Étape 5 : lancer le test rapide
1. Onglet **Actions** → dans la colonne de gauche, **« Générer les boucles vélo »** → **Run workflow**.
2. **Laisse les valeurs par défaut** (3 départs, 1 h, Barcelone + Vallès) → **Run workflow**.
3. Clique sur la ligne qui apparaît pour suivre l'avancement en direct.
4. Durée : non mesurée (je n'ai pas pu tester dans GitHub). Prévois jusqu'à environ 1 h pour ce premier essai.

**✔ Vert** : passe à l'étape 6.
**✖ Rouge** : c'est fréquent au premier essai. Clique sur l'étape en rouge, copie les 30 dernières lignes du journal
et colle-les dans la conversation avec Claude : c'est la manière normale de corriger.

### Étape 6 : regarder le résultat
Dans le dépôt, ouvre `web/data/index.json` et `web/data/starts/` (un fichier par ville de départ). Envoie-les dans
la conversation avec Claude pour les vérifier ensemble : distance, dénivelé et durée te paraissent-ils cohérents avec
les routes que tu connais ?

### Étape 7 : génération complète (seulement quand le test est bon)
**Run workflow** avec `max_starts` = 30, `durations` = `1 2 3` et le champ `bbox` **vidé** (toute la province).

---

## 3. Modifier un fichier sans rien installer
Dans ton dépôt, appuie sur la touche **`.`** (point) : un éditeur s'ouvre dans le navigateur. Tu peux aussi cliquer
sur le crayon ✏ d'un fichier. Exemple : changer les villes de départ dans `config/region_barcelona.json`.
Pour enregistrer : **Commit changes**.

---

## 4. Dépannage

- **Aucun workflow n'apparaît dans l'onglet Actions** : `.github` n'est pas à la racine du dépôt (voir étape 4.5).
- **`GraphHopper s'est arrêté`, `OutOfMemory` ou `Killed`** : mémoire insuffisante. Réduis le champ `bbox`.
- **`curl` échoue au téléchargement** : réseau ponctuel, relance le workflow.
- **« Le serveur GraphHopper n'a pas de données d'altitude »** : le téléchargement des tuiles d'altitude a échoué.
  Relance ; si ça persiste, envoie le journal à Claude.
- **`départs introuvables dans les données OSM`** : un nom de ville ne correspond pas à OpenStreetMap (noms en
  catalan). Corrige-le dans `config/region_barcelona.json`.
- **L'étape « Committer » échoue (erreur 403)** : Settings → Actions → General → *Workflow permissions* →
  **Read and write permissions** → Save, puis relance.
- **Peu d'options pour un départ** : normal dans certains cas (départ en ville dense, durée longue). Le journal indique
  le nombre de candidats valides par combinaison.

---

## 5. Comment ça marche (en bref)
1. Le workflow télécharge l'extrait OSM de Catalogne (Geofabrik) et le découpe sur la zone.
2. Il démarre GraphHopper 11.0 dans le cloud (import OSM + altitude SRTM + % de zone bâtie).
3. Pour chaque départ × durée × niveau, il demande des boucles (`round_trip`) et ajuste la distance pour viser la durée
   (modèle watts → vitesse **interne, jamais affiché**).
4. Il calcule des métriques réelles, note les candidats, garde jusqu'à 3 options (équilibrée / plate / vallonnée)
   et écrit `web/data/`. Le pitch n'est construit qu'à partir de ces métriques mesurées.

### Format de sortie
- `web/data/index.json` : région, date des données OSM, liste des départs.
- `web/data/starts/<depart>.json` : options (`distance_km`, `ascend_m`, `time_est_min`, `shares`, `overlap`, `score`,
  `pitch`, `wind_bins_km`, `coords` [lon, lat, altitude]).
- `wind_bins_km` : km parcourus dans 8 secteurs (N, NE, E…) sur la 1re et la 2e moitié de la boucle. Le navigateur les
  compare à la direction du vent (Open-Meteo) pour classer les boucles, sans recalculer d'itinéraire.

### Réglages
- `scripts/generate_loops.py` : poids du score (`WEIGHTS`), niveaux et watts internes (`LEVELS`), masse, CdA, facteur
  « monde réel » (`REAL_WORLD_FACTOR`, à calibrer avec tes sorties).
- `config/custom_models/` : préférences de routage (éviter la ville, les axes principaux, favoriser le réseau cyclable).
- `config/region_barcelona.json` : départs (noms OSM en catalan), durées, niveaux, emprise (`bbox`, approximative).

---

## 6. Licences et données
- Données © contributeurs OpenStreetMap (ODbL) : afficher l'attribution dans l'app et dans les GPX.
- Les tracés générés sont des œuvres produites. **Ne commite pas** `graph-cache/` ni une base par tronçon enrichie d'OSM
  dans un dépôt public (ce serait une base dérivée soumise à l'ODbL).
- Le dépôt est public : n'y mets jamais de mot de passe, de clé d'accès ni de donnée personnelle.
- Altitude : tuiles SRTM publiques (AWS « skadi »). GraphHopper : licence Apache 2.0.
- Les calculs sont gratuits sur un dépôt public (runners GitHub standard). Les conditions de GitHub peuvent évoluer.

## 7. Ce qui a été testé, et ce qui ne l'a pas été
**Testé** : le script complet contre GraphHopper 11.0 sur un extrait OSM d'Andorre **sans altitude**, les commandes
osmium, la syntaxe du workflow, le modèle physique (vitesses plausibles à 110 / 150 / 190 W).
**Non testé** : le téléchargement Geofabrik, le téléchargement des tuiles d'altitude, la durée et la mémoire réelles
de l'import sur Barcelone, l'exécution du workflow sur GitHub, et le calage durée / D+ en relief.
Le script refuse de tourner si le serveur n'a pas d'altitude (sauf `--allow-no-elevation`, réservé aux tests).

---

## 8. Avancé (facultatif) : lancer sur son propre ordinateur
Nécessite Java 21, Python 3.12 et osmium-tool. Inutile pour l'instant.
```bash
curl -L -o data/source.osm.pbf https://download.geofabrik.de/europe/spain/cataluna-latest.osm.pbf
osmium extract --bbox 1.30,41.20,2.75,42.35 --strategy=smart data/source.osm.pbf -o data/region.osm.pbf
osmium tags-filter data/region.osm.pbf n/place=city,town,village -o data/places.osm.pbf
osmium export data/places.osm.pbf -f geojson -o data/places.geojson
java -Xmx6g -Ddw.graphhopper.datareader.file=data/region.osm.pbf -jar graphhopper-web.jar server config/graphhopper.yml
# dans un autre terminal, quand http://localhost:8989/health répond OK :
pip install -r scripts/requirements.txt
python scripts/generate_loops.py --region config/region_barcelona.json --places data/places.geojson --max-starts 3 --durations 1
```
