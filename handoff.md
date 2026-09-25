# HANDOFF — velo-loops (application de parcours vélo pour cyclistes amateurs)

> Dernière mise à jour : 25 septembre 2026
> Porteur du projet : Florent (débutant en code, travaille en « vibe coding » avec Claude)
> À lire au début de chaque session, avec `rules.md`. Détails produit : `docs/product.md`. Journal des décisions : `docs/decisions.md`.

✅ **O-1 (audit du dépôt) réalisé le 23/09/2026** : tous les [À VÉRIFIER] ci-dessous sont confirmés par lecture du code, comparée au site réellement publié.

⚠️ **Risque permanent à vérifier à chaque session** : le dépôt Git et le site publié peuvent diverger (le pipeline ne committe jamais les données générées, mais des envois manuels via GitHub l'ont déjà fait). Ne jamais juger de l'état réel de l'app sur la seule lecture du dépôt local — croiser avec le site publié. Détail et plan de nettoyage : voir tâche **O-8**.

---

## 1. En une phrase

Une web app qui aide un cycliste amateur à **découvrir où s'entraîner** : il donne un point de départ, une durée et un niveau, et reçoit plusieurs boucles agréables (peu de ville, pistes cyclables, paysages), chacune avec carte, km, dénivelé, % en ville et un « pitch » qui explique les choix, exportables en GPX vers sa montre ou son compteur.

## 2. Objectifs

| Horizon | Objectif |
|---|---|
| Maintenant (POC) | Prouver que des boucles pré-calculées sur données ouvertes sont *agréables* et *crédibles* pour un cycliste qui connaît le terrain (province de Barcelone). Coût : 0 €. |
| Ensuite (MVP) | Départ depuis n'importe quelle adresse + n'importe quelle durée, feedback après sortie, personnalisation simple (3-4 règles). |
| Lancement | Une seule région : la France. Web app avant app native. |
| Plus tard | Social (partenaires d'entraînement), multi-sport (course, trail, VTT), modèle économique (pub sport ou abonnement). |

---

## 3. SECTION FIGÉE — décisions actées

*Ne pas remettre en cause sans une raison explicite, et consigner tout changement dans `docs/decisions.md`.*

### 3.1 Positionnement
- **Outil de découverte, pas programme d'entraînement.** On dit *où* rouler, pas *comment* s'entraîner.
- Usage cible : surtout autour du domicile, aussi en déplacement.
- Historique utilisateur (pour la « nouveauté ») : **on part de zéro**, pas d'import Strava ; il se construit au fil des sorties.

### 3.2 Périmètre du POC
- Zone de test : **province de Barcelone et alentours** (~885 départs pré-calculés au 23/09/2026, scénario dmax 1,75/2,5/2,5 km — densifié depuis les ~715 du 22/09, scénario 2/3/3, voir O-3). Les départs ruraux hors province sont conservés ; le front **ne présente pas** la province comme limite de la zone couverte.
- **Boucles pré-calculées** (départ pré-calculé le plus proche + durées à choix). Le routage à la demande par serveur est **repoussé**.
- **Pas de GPS intégré** : export **GPX** vers Garmin, Wahoo, Apple Watch, Strava.
- **Watts** : calcul interne uniquement pour estimer la difficulté, jamais affichés.
- **Personnalisation** : 3-4 règles simples, **pas de machine learning** au départ.
- **Repoussés** : social, publicité, multi-sport, app native, test utilisateur formel.

### 3.3 Stack technique (0 €)
| Élément | Choix |
|---|---|
| Hébergement code | Dépôt GitHub **public** `velo-loops` : https://github.com/florentbedouret-lgtm/velo-loops |
| Données | OpenStreetMap (gratuit, open source) |
| Routage | GraphHopper, exécuté dans **GitHub Actions** pour pré-calculer les boucles |
| Stockage des boucles | JSON. `web/data/index.json` (méta + liste des départs) et `web/data/starts/<id>.json` (options par départ, ~35-155 Ko selon le nombre d'options). Généré par le workflow et publié **directement** sur GitHub Pages — normalement jamais committé dans le dépôt. |
| Front | Web app statique sur **GitHub Pages** (= version de travail du POC) |
| Géocodage | **Photon** (géocodeur public de Komoot) pour la recherche d'adresse |
| Localisation | API de géolocalisation du navigateur |

> ✅ **Désynchronisation constatée le 23/09/2026, corrigée le 23/09/2026 (O-8)** : `web/data/` et `web/approach_model.json` committés dans le dépôt étaient un reste d'un envoi manuel du 20/09 (3 départs seulement, modèle d'approche v2.0, et même incohérents entre eux). Correctifs appliqués : `web/approach_model.json` resynchronisé avec le site publié (v2.2, 715 départs) ; `web/data/` retiré du suivi Git (`.gitignore`, régénéré à chaque publication) ; `scripts/fetch_published.py` retélécharge désormais aussi `web/approach_model.json` en mode `site`, pour que ce type de dérive ne puisse plus se reproduire silencieusement au prochain push front-end.

### 3.4 Fonctionnalités en ligne et fonctionnelles
- Carte, choix du départ et du niveau.
- Plusieurs options de boucle avec pitch et liste des montées.
- Export GPX (testé).
- Recherche d'adresse (Photon) + géolocalisation.
- Calcul de distance et de **temps d'approche** jusqu'au départ pré-calculé le plus proche.
- Mentions légales et confidentialité (`mentions.html`, lien discret dans l'app).

### 3.5 Règle de distance au départ (reco UX retenue)
- Distance max à vol d'oiseau : **1,75 km en zone dense, 2,5 km en périphérie, 2,5 km en rural** (resserré depuis 2/3/3 km le 23/09/2026, voir D16 et O-3).
- Affichage en **temps d'approche aller**, pas en km ; au-delà de **30 min → « trop loin »**.

### 3.6 Principes produit non négociables
- Ne promettre que ce qui est **calculable**. Le pitch est **généré à partir de métriques réelles** (ex. « 62 % sur piste cyclable », pas « magnifique route calme » sans donnée).
- Trafic et beauté ne sont pas dans OSM → utiliser des **proxys** (type de route ; forêt, eau, parcs ; densité de photos).
- RGPD : **minimiser** localisation et poids. Mentions claires de responsabilité (parcours à vérifier par le cycliste).

### 3.7 Arborescence du dépôt — confirmée le 23/09/2026
```
velo-loops/
├── .github/workflows/build-loops.yml   # unique workflow (8 modes : plan/detour/pilot/full/site/...)
├── brand/                              # logos, icônes, favicons, oyan-tokens.css (charte Oyan v1, D19)
├── fonts/                              # polices Oyan hébergées (Hanken Grotesk, IBM Plex Mono) + licences OFL
├── config/                             # région, config GraphHopper, modèles de routage
├── scripts/                            # pipeline Python réel : plan_starts, generate_loops,
│                                        #   measure_detour, build_approach_model, fetch_published…
├── index.html, gpx.js, profile.js, feedback.js   # front (vanilla JS, sans build, MapLibre via CDN)
├── web/approach_model.json             # publié à chaque run ; suivi en Git (petit fichier)
├── web/data/                           # généré à chaque run, jamais committé (.gitignore, O-8)
├── .gitignore
├── CLAUDE.md
├── handoff.md
├── rules.md
└── docs/
    ├── product.md
    └── decisions.md
```

---

## 4. SECTION OUVERTE — tâches de la session en cours

*Cette section est vivante : la mettre à jour à la fin de chaque session (fait / en cours / suivant).*

### État au 23/09/2026 (après audit O-1)
Le POC tourne en ligne et est plus avancé que ce document ne le disait : recherche d'adresse, géolocalisation, départ le plus proche avec règle dmax/temps d'approche, boucles avec pitch 100 % mesuré, détection des demi-tours/recouvrements, export GPX, feedback local après sortie — tout est codé et vérifié dans le code (voir tâches recochées ci-dessous). Le vrai frein n'est plus le code mais le dépôt Git lui-même : **toujours croiser `git pull` avec le site publié**, pas seulement l'un ou l'autre (voir avertissement §3.3).

### Tâches
- [x] **O-1 — Audit du dépôt** — fait le 23/09/2026 : arborescence, format des données, workflow, profils GraphHopper, niveaux/durées confirmés dans le code ; désynchronisation dépôt/site publié découverte (voir O-8).
- [x] **O-2 — Règle dmax/temps d'approche (3.5)** — déjà codée, vérifié en O-1 : `DMAX_KM` et le calcul du temps d'approche sont dans `index.html` (zones dense/périphérie/rural, seuil 30 min).
- [x] **O-3 — Densifier la couverture** — fait, 23/09/2026 :
  - **Départs** : resserrés de 2/3/3 km à 1,75/2,5/2,5 km (dense/périphérie/rural), 715 → 889 départs en ligne (+24 %). Gain réel confirmé par la remesure `detour` : **P90 temps d'approche rural 35,35 → 29,04 min** (le pire cas repasse sous le seuil de 30 min pour plus de monde). `DMAX_KM` (index.html) aligné sur le nouveau scénario. `web/approach_model.json` reconstruit en v2.3 via le nouveau script `scripts/build_approach_model.py` (résout le point resté ouvert d'O-8 : ce fichier n'est plus édité à la main).
  - **Durées** : ajout de 45 min et 4 h (liste complète : 0,75/1/1,5/2/3/4 h). Run complet en 219 min (budget 300 min, 73 %) — `reuse` n'a rien réutilisé cette fois (`starts_reused: 0` ; il fonctionnait par départ entier, pas par combinaison durée/départ). **Corrigé le 23/09/2026** dans `scripts/generate_loops.py` : `reuse_candidate()` ne vérifie plus que toutes les durées demandées sont déjà publiées ; `process_start()` réutilise maintenant les durées déjà calculées et ne recalcule que les manquantes, pour n'importe quel départ. Testé unitairement (intersection de durées, niveau absent, clé inconnue, âge dépassé) faute de pouvoir lancer GraphHopper en local — **pas encore validé sur un vrai run**, à surveiller au prochain `full`/`generate_planned` avec `reuse`.
  - **Estimateur de `plan_starts.py`** corrigé le 23/09/2026 : `SEC_PER_START` était une estimation (80-190 s/départ) jamais recalée, 2 à 5× trop pessimiste face au réel mesuré (35-38 s/départ pour 4 durées, `stats.seconds_per_computed_start_by_zone` des runs du 22-23/09). Remplacé par les valeurs mesurées (35-65 s, `KB_PER_START` 150 Ko). Le mode `plan` donnera des temps de calcul estimés bien plus fiables pour les prochaines décisions de densification.
  - Une régression a été trouvée et corrigée en cours de route : voir l'entrée O-8 ci-dessus.
- [x] **O-4 — Qualité des boucles** — déjà fait, vérifié en O-1 : `scripts/generate_loops.py` rejette les candidats à plus de 25 % de tronçons répétés ou plus de 2 demi-tours, pénalise le score (poids « flow »), et le signale dans le pitch. Reste ouvert : régler/affiner ces seuils si des boucles publiées paraissent encore mauvaises en pratique.
- [x] **O-5 — Pitch fiabilisé** — déjà fait, vérifié en O-1 : `pitch_from_option()` ne produit que des phrases adossées à un champ mesuré exporté (aucune affirmation non vérifiable).
- [x] **O-6 — Feedback après sortie** — déjà fait, vérifié en O-1 : `feedback.js` capture note, minutes réelles, D+ réel, difficulté ressentie, tags +/−, commentaire ; stocké en `localStorage`, exportable en JSON, rien n'est envoyé. Décision « localStorage » prise de fait.
- [x] **O-7 — Mentions légales / responsabilité et RGPD** — fait le 23/09/2026 : page `mentions.html` (responsabilité sur les parcours à vérifier, données personnelles minimisées, attributions OSM/GraphHopper/OpenFreeMap/Photon, éditeur identifié en pseudonyme + contact GitHub), liée depuis `index.html`. ⚠️ Rédaction niveau POC, pas relue par un juriste — à faire avant un lancement plus large ou si le trafic augmente.
- [x] **O-8 — Nettoyer le dépôt (dette trouvée en O-1)** — fait le 23/09/2026, avec une régression corrigée en cours d'O-3 (voir ci-dessous) :
  1. ⚠️ Tentative de resynchronisation de `web/approach_model.json` — **ratée** : Claude a lu `web/approach_model.json` (racine, le bon, v2.2) et `web/data/approach_model.json` (copie périmée, v2.0) puis a copié dans le mauvais sens, écrasant le bon avec le périmé. Le commit d'O-8 a donc dégradé le fichier au lieu de le réparer, sans qu'aucun `diff` post-copie ne le révèle (il compare « identiques ? », pas « lequel est correct »). Non détecté sur le coup car ce commit ne touchait pas `index.html` : pas de republication immédiate, donc le site est resté sur son ancien déploiement (toujours bon) le temps qu'une vérification donne un faux positif.
  2. ✅ `scripts/fetch_published.py` avait été modifié pour retélécharger aussi `web/approach_model.json` depuis le site publié en mode `site` — **c'est cette mécanique qui a propagé la régression** (run #42, 23/09/2026) : elle a réécrasé un commit v2.3 correct avec le v2.0 périmé encore servi par le site à ce moment-là. **Revert** : ce fichier est de nouveau traité comme n'importe quel fichier du front (`index.html`, etc.) — republié tel quel depuis le dépôt Git, jamais retéléchargé depuis le site en ligne. Le dépôt Git est la source de vérité, pas le site déjà publié (surtout maintenant qu'il est reproductible, voir O-3/`build_approach_model.py`).
  3. ✅ Doublon `generate_loops.py` (racine) supprimé.
  4. ✅ `.gitignore` ajouté ; `web/data/` retiré du suivi Git (fichiers gardés sur le disque de Florent, juste plus committés). **`web/data/approach_model.json` supprimé aussi** (copie redondante sans usage réel — c'est elle qui a causé la confusion du point 1 ; il n'y a plus qu'un seul exemplaire, `web/approach_model.json`).
  **Leçon retenue** : après toute action affectant le site publié, vérifier le résultat avec un en-tête `Last-Modified`/`X-Cache` (`curl -I`), pas seulement le contenu — un contenu qui semble correct peut venir d'un déploiement antérieur non encore remplacé par la CDN.
- [ ] **O-9 — Identité Oyan** — nom (D18) et identité visuelle v1 « Argile » (D19) actés et consignés dans `docs/brand.md` §12-13 le 25/09/2026. Reste à faire :
  1. ✅ Exports versés dans `brand/` le 25/09/2026 (10 SVG, 13 PNG, `oyan-tokens.css`, `logo-paths.json`, `README.md`) — sans le moodboard ni les maquettes HTML. Métadonnées C2PA conservées dans ces originaux.
  2. ✅ Charte appliquée à l'app le 25/09/2026 (5 commits, runs #46 à #50, chacun vérifié en ligne avec `curl -I`) : (a) tracé argile 4,5 px + liseré blanc 8,5 px, autres boucles en galet, sous les noms de lieux ; marqueurs départ argile / position basalte ; (b) couleurs via `brand/oyan-tokens.css` (lié, pas copié), un seul bouton argile (GPX), les autres en secondaire, alertes basalte + ⚠️ ; (c) polices dans `fonts/` (Hanken Grotesk variable 61 Ko + IBM Plex Mono Latin1 17 Ko, licences OFL jointes, zéro appel à Google) ; (d) titre « Oyan », favicon/apple-touch-icon pointant vers `brand/` (C2PA gardé, pas de copie), « Oyan » dans le GPX ; (e) `mentions.html` aligné. Le workflow publie désormais aussi à chaque push de `mentions.html`, `brand/**`, `fonts/**` (avant, `brand/` n'était pas en ligne). Écarts assumés à la charte : voir D20.
  3. ✅ (partiel) Lisibilité à 16 px : le favicon ne contient que le signe (sans « yan »), donc pas de risque « Cyan » dans l'onglet. Reste à tester le **logo complet** quand il apparaîtra dans l'app, auprès de personnes qui ne connaissent pas le nom.
  4. Vérifier si le pipeline calcule les % ville/forêt/eau/campagne avant d'afficher la barre de terrain (expert dev).
  5. Ton des textes de l'app (pitch, boutons) selon `docs/brand.md` §8 ; en profiter pour ajouter des libellés en mono capitales dans la fiche boucle (aujourd'hui un seul : « Durée de la boucle »).
  6. Mise en page de la fiche boucle selon la charte (chiffre principal en Hanken Light 48 px, une information principale par écran) — dev front / UX.
- [ ] **O-10 — Liste des départs utilisable** (demande de Florent, 25/09/2026) : 889 départs dans le désordre, 104 noms affichés en double (« Près de Terrassa » × 5), quartiers sans leur ville (« Sant Martí », « l'Eixample »). État :
  1. ✅ Tri alphabétique (`Intl.Collator('ca')`, accents ignorés) — commit d0bcf67, run #51, vérifié en ligne.
  2. ✅ (partiel) « Ville · lieu » pour les 403 départs `fill` (« Sabadell · Can Llong (sud-est) »), aussi dans le message « Départ : … » — même commit ; plus aucun doublon. **Reste** : la ville affichée est la plus proche, pas forcément la commune, et les quartiers (« Gràcia ») n'ont pas « Barcelona · ». Version propre : champ `municipality` (commune OSM, admin_level 8, point-dans-polygone) dans `plan_starts.py`, puis régénérer les noms (mode `generate_names`), le front n'aura qu'à l'afficher. Expert dev pipeline.
  3. ✅ Champ de filtre au-dessus de la liste (plusieurs mots, sans accents ni majuscules ; se vide si un clic sur la carte choisit un départ masqué) — commit 4046aca, run #52, vérifié en ligne. Choisi plutôt que `<datalist>`, mal géré sur iPhone.
- [ ] **O-11 — Profil relié à la carte** (demande de Florent, 25/09/2026) : glisser le doigt sur le profil altimétrique affiche le point correspondant sur la carte (+ trait vertical et « km · altitude » sur le profil). Estimé ~40 lignes (`profile.js`, `index.html`). Point délicat : conflit avec le défilement vertical sur mobile (`touch-action: pan-y`) — à tester sur un vrai téléphone.
- [ ] **O-12 — Sortie vers la nature pour les départs de centre-ville** (question de Florent, 25/09/2026, exemple de Gràcia) — expert dev pipeline. Constat sur les 36 boucles publiées de `gracia` (départ 82 m) : jusqu'à 2 h, aucune n'entre dans Collserola (61-100 % en ville, point haut 88-254 m) ; à 3 h, presque toutes y vont (dès le km 2,4, point haut 431 m, 28-44 % en ville) ; 0 % non goudronné partout. Causes identifiées dans le code :
  1. Coût réel de la montée (+350 m) : à 110 W, ~40 min de montée → pour « tranquille » < 1 h 30, rester en ville est défendable.
  2. `bike_elevation.json` (GraphHopper 11.0, profils calm et sport) ralentit les montées (×0,9 dès 4 %, ×0,8 dès 8 %) : le routeur préfère le plat, donc la ville.
  3. Génération aléatoire : 10 candidats `round_trip` (2 seeds + 8 caps) ; pour 15-25 km le point de passage tombe à 4-8 km, encore en ville ; le score ne choisit qu'entre ces 10, rien ne vise activement le vert.
  4. Score : `calm` 22 %, `scenery` 12 %, rien sur « rejoindre la nature vite ».
  **Avis** : erreur réelle pour « sportif » 1 h-1 h 30 (Arrabassada/Vallvidrera = sortie classique, ~20 min de montée à 190 W). Probablement le cas d'autres départs urbains au pied d'un relief (non vérifié).
  **Plan** : (a) d'abord un run de diagnostic sur ~10 départs de centre-ville (tous les candidats, scores, raisons de rejet), comme `slope_check` ; (b) ajouter 2-3 candidats forcés via l'entrée de zone verte la plus proche (index `LANDSCAPE` déjà chargé) pour les départs `dense` ; (c) retirer/adoucir `bike_elevation` du profil sport (le temps reste calculé par notre modèle physique) ; (d) intégrer le temps de sortie de ville (`exit_dense_km`) au score. Budget : dernier run complet 219/300 min ; (b) ajoute ~20-30 % sur les départs `dense` → envisager de découper le run.
  **Référence terrain à demander à Florent** : la boucle qu'il ferait lui-même depuis Gràcia en 1 h 30 à bon rythme, pour juger si la correction marche.

### Backlog des idées retenues (après POC, à prioriser)
Vent/météo pour orienter la boucle · type de séance (endurance, intervalles avec côte 5-6 min, sortie plaisir) · échappatoires (raccourci fatigue/crevaison/orage, points d'eau, boulangeries, gares) · difficulté en langage humain · curseur de fréquentation · carte à dévoiler (zones blanches) · micro-aventure train + vélo (A→B entre deux gares) · guide audio IA des lieux traversés. Détails : `docs/product.md`.

### Questions ouvertes
- Modèle économique (pub produits sport vs abonnement) — à traiter avec un expert business.
- Disponibilité juridique du nom « Oyan » (marque INPI/EUIPO, nom de domaine) — juriste propriété intellectuelle, avant d'investir davantage dans le nom.
- `mentions.html` dit que la position n'est « jamais envoyée à un serveur » ; or la carte est chargée depuis unpkg.com et OpenFreeMap, qui reçoivent l'IP et les tuiles consultées (≈ zone regardée). À faire relire par un juriste (avec la relecture prévue en O-7) ; option technique : héberger MapLibre dans le dépôt comme les polices (architecte).
- Conditions d'accès API Strava/Garmin à revérifier avant toute intégration.
- Quand passer du pré-calcul au routage à la demande (et à quel coût).
- Stratégie de bascule Barcelone → France.

---

## 5. Journal de session (à compléter en fin de session)

| Date | Rôle d'expert | Fait | Suivant |
|---|---|---|---|
| sept. 2026 | Architecte / dev | Dépôt `velo-loops`, premier run GitHub Actions réussi | Front |
| sept. 2026 | Dev front-end | Web app sur Pages : carte, niveaux, options, pitch, montées, GPX | Recherche d'adresse |
| sept. 2026 | Dev front-end + UX | Photon + géoloc + départ le plus proche + temps d'approche ; ~715 départs ; règle dmax | O-1 à O-7 |
| 23/09/2026 | Dev / Architecte | O-1 : audit complet (code vs site publié) ; O-2/O-4/O-5/O-6 recochées comme faites ; désync dépôt/site trouvée et documentée (O-8) | Décider priorité entre O-8 (nettoyage) et O-3/O-7 |
| 23/09/2026 | Dev / Architecte | O-8 : nettoyage dépôt (commité et poussé) ; O-7 : page `mentions.html` + lien dans `index.html` (pas encore committé) | Committer O-7, puis O-3 |
| 23/09/2026 | Dev / Architecte | O-3 (en cours) : scénario de départs resserré à 1,75/2,5/2,5 km, 885 départs en ligne ; `scripts/build_approach_model.py` créé, `web/approach_model.json` reconstruit en v2.3 ; committé et poussé (eb13234) | Vérifier le déploiement, puis durées + suite |
| 23/09/2026 | Dev / Architecte | Régression trouvée : le commit O-8 avait écrasé `web/approach_model.json` (v2.2 → v2.0) par erreur d'inversion de fichiers ; `fetch_published.py` propageait le v2.0 périmé à chaque run `site`. Fichier dupliqué supprimé, mécanique revert, commité (08547fa, 4d81851), redéployé (run #43) et vérifié en direct (v2.3, headers frais) | O-3 : volet durées |
| 23/09/2026 | Dev / Architecte | O-3 terminée : durées 0,75/1/1,5/2/3/4 h générées et publiées (run #44, 889 départs, 219 min/300, reuse inefficace ici) ; vérifié en direct | Choisir la prochaine tâche (O-4 à O-7 restants, ou nouvelle priorité) |
| 23/09/2026 | Dev / Architecte | Diagnostic pente (`slope_check`, run manuel, 59 boucles) : confirme le choix du lissage 500 m et l'absence de % affiché (D17) ; seuil du badge gardé à 20 %. `reuse` corrigé pour fonctionner par durée (pas encore validé sur un run réel) ; estimateur `plan_starts.py` recalé sur les mesures | Validation terrain du POC (reste bloquée côté Florent) ; identité/nom si besoin |
| 25/09/2026 | Branding / DA | Nom Oyan (D18) et identité visuelle v1 « Argile » (D19) consignés dans `docs/brand.md` §12-13, à partir de l'export Claude Design ; `.claude/` ajouté au `.gitignore` | O-9 : verser les exports dans `brand/`, puis appliquer la charte à l'app |
| 25/09/2026 | Branding / DA | O-9 étape 1 : dossier `brand/` créé (logos, icônes, favicons, tokens CSS), sans images du moodboard ; §3.7 complété (accord de Florent) | O-9 étape 2 : appliquer la charte à l'app (rôle dev front / UX) |
| 25/09/2026 | Dev front / UX | O-9 étape 2 : charte Oyan appliquée à l'app en 5 commits publiés et vérifiés (tracé, couleurs, polices hébergées, titre/favicon, mentions) ; workflow étendu à `mentions.html`/`brand/`/`fonts/` ; D20 ; demandes O-10 (liste des départs) et O-11 (profil ↔ carte) ajoutées, puis O-12 (boucles de centre-ville qui ne rejoignent pas la nature, analyse du cas Gràcia) ; §3.7 : `fonts/` ajouté (accord de Florent). Serveur local : Python 3.6 bloquait, remplacé par `.claude/serve.py` (local, non versionné) | O-10 (tri + « Ville · départ ») ou O-11 ; test du rendu sur le téléphone de Florent |
| 25/09/2026 | Dev front / UX | O-10 : liste des départs triée, « Ville · lieu » pour les départs intermédiaires (0 doublon sur 889), champ de filtre ; 2 commits publiés et vérifiés (runs #51, #52) | O-10.2 version propre (commune, pipeline) avec O-12 ; ou O-11 (profil ↔ carte) |
