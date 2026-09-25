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
- [x] **O-10 — Liste des départs utilisable** (demande de Florent, 25/09/2026) — fait le 25/09/2026. Avant : 889 départs dans le désordre, 104 noms en double, quartiers sans leur ville.
  1. ✅ Tri alphabétique, regroupé par commune (`Intl.Collator('ca')`) — d0bcf67, run #51.
  2. ✅ « Commune · lieu » : champ `municipality` (commune OSM admin_level 8, lue dans l'extrait Catalogne NON découpé, tolérance 300 m) ajouté par `plan_starts.py` et transmis par `generate_loops.py` ; publié par le run `full` #57 : 877 départs, **tous avec leur commune**, 0 doublon (« Barcelona · Gràcia »). Gares : « Calella · Gare », « Granollers · Gare Centre » (f91dc02, run #59). Commits a641dd9, 1280942.
  3. ✅ Champ de filtre (plusieurs mots, sans accents) au-dessus de la liste — 4046aca, run #52. Choisi plutôt que `<datalist>` (mal géré sur iPhone).
- [x] **O-11 — Profil relié à la carte** (demande de Florent, 25/09/2026) — fait le 25/09/2026 (c6939ff, run #53) ; **testé OK sur le téléphone de Florent**. Glisser le doigt/la souris sur le profil : trait vertical, « km · altitude » et point basalte sur la boucle (précision 0,5 m). `touch-action: pan-y`. Reste possible : le bloc d'attribution de la carte (~60 px) peut masquer le point sur mobile ; attribution compacte à vérifier d'abord avec les exigences OSM.
- [x] **O-12 — Sortie vers la nature pour les départs de centre-ville** (question de Florent, 25/09/2026, exemple de Gràcia) — **fermée côté génération le 25/09/2026, voir D21**. Mode de diagnostic `nature_check` (rien n'est publié), 5 versions :
  - v1-v2 (12 départs urbains) : des candidats « vers le vert » (triangle vers l'espace vert le plus proche) existent mais ne gagnent jamais (0/45) ; ils ne sont pas moins urbains (51 % contre 50 %) et perdent surtout sur les tronçons répétés (aller-retour sur la route d'accès). « Côtes non freinées » : rebat les cartes sans améliorer (abandonné).
  - v3 (boucle préférée de Florent : Via Augusta, col de Vallvidrera, Arrabassada ; 22 km, ~470 m D+) : perd de 19 points contre la boucle actuelle (grands axes −11, répétitions −5, pistes −3/−5) bien qu'au bord de la forêt sur 50 % (contre 19 %).
  - v4 : rayon « ville » de GraphHopper à 500 m au lieu de 1 500 (défaut, jamais réglé) : part en ville médiane 50 % -> 39 %, mais la boucle de Florent reste à 80 % (Sarrià, Vallvidrera, Tibidabo vus comme réseau dense) ; « grands axes v2 » (demi-pénalité hors ville) sans effet (2 % de grandes routes classées hors ville).
  - v5 (**jeu de 33 boucles de référence**, `scripts/reference_loops.json` : 32 itinéraires populaires Komoot + la variante de Florent, points de passage seulement) : sur des critères neutres, les boucles d'Oyan valent les populaires depuis les mêmes départs (médianes : ville 10 % contre 19 %, forêt 45 % contre 47 %, D+ 666 contre 606 m, grandes routes 10 % contre 20 %). Seul écart systématique : les populaires repassent par les mêmes routes (8 % en médiane, aussi dans les traces d'origine), pénalisées −6 points. Pondérations testées : actuelle 11/63 boucles populaires gagnantes, grands axes ÷2 12, paysage ×2 16, répétitions tolérées 18, combiné 22.
  - **Réserve pour le prochain recalcul complet** (qui aura une autre raison d'être) : pénaliser moins les tronçons répétés (facteur 2 -> 1 dans `score()`), seul ajustement appuyé par les données. Le rayon « ville » 500 m reste à vérifier sur une carte (le quadrillage de Barcelone doit rester « ville ») avant toute adoption : il change aussi les « % en ville » affichés (principe 3.6).
  - Le problème de Gràcia est local (centre dense, colline à 2 km, accès par de grands axes) : réponse côté app, voir O-14.
- [x] **O-13 — Plan des départs stable** (trouvé au run `full` #57 du 25/09/2026 : 84 départs intermédiaires déplacés ou remplacés en 2 jours d'évolution d'OSM, 889 -> 877) — fait le 25/09/2026 (e379985, voir D22). Le mode `full` garde désormais les départs publiés (`scripts/published_starts.py`, clés de réutilisation conservées) ; nouveau mode `full_replan` pour refaire le plan volontairement. **Validé par le run `full` #65 (25/09/2026)** : 877 départs avant et après, mêmes clés, aucun déplacé. 791 réutilisés en entier ; les 86 autres n'avaient que 1 à 5 durées et le pipeline retente les durées manquantes à chaque run (aucune gagnée, ~8 min) : optimisation possible, retenir les durées impossibles. À faire encore : vérifier si la couverture a baissé avec 877 départs (rapport `coverage.json`).
- [x] **O-14 — Proposer un départ voisin qui sort plus vite de la ville** (dev front / UX) — fait le 25/09/2026 (0a3c4c0, run #66, vérifié en ligne). Quand la boucle reste en ville ou n'en sort qu'après 5 km : « Plus vite dans la nature : depuis <voisin>, à ≈ N min à vélo, la boucle de 1 h 30 sort de la ville après X km. [Voir ce départ] ». Voisin : même durée et niveau, approche ≤ 25 min **montée comprise** (max(temps à plat, dénivelé / 400 m/h) ; altitude `alt_m` ajoutée à l'index par 4287a0f, publiée au run #65), sortie de ville ≥ 3 km plus tôt. Sans altitude connue, rien n'est proposé. Gràcia 1 h 30 : Horta-Guinardó à 11 min (36 % en ville au lieu de 67 %) ; le Tibidabo (430 m plus haut, ~64 min) n'est plus proposé. Suggestion présente dans 100 des 106 cas concernés (1-2 h) : durcir le seuil de 3 km si c'est trop fréquent à l'usage.

### Backlog des idées retenues (après POC, à prioriser)
Vent/météo pour orienter la boucle · type de séance (endurance, intervalles avec côte 5-6 min, sortie plaisir) · échappatoires (raccourci fatigue/crevaison/orage, points d'eau, boulangeries, gares) · difficulté en langage humain · curseur de fréquentation · carte à dévoiler (zones blanches) · micro-aventure train + vélo (A→B entre deux gares) · guide audio IA des lieux traversés. Détails : `docs/product.md`.

### Questions ouvertes
- Modèle économique (pub produits sport vs abonnement) — à traiter avec un expert business.
- Disponibilité juridique du nom « Oyan » (marque INPI/EUIPO, nom de domaine) — juriste propriété intellectuelle, avant d'investir davantage dans le nom.
- `mentions.html` dit que la position n'est « jamais envoyée à un serveur » ; or la carte est chargée depuis unpkg.com et OpenFreeMap, qui reçoivent l'IP et les tuiles consultées (≈ zone regardée). À faire relire par un juriste (avec la relecture prévue en O-7) ; option technique : héberger MapLibre dans le dépôt comme les polices (architecte).
- `scripts/reference_loops.json` reprend des points de passage d'itinéraires publiés sur Komoot (pas les traces) ; usage jugé à faible risque, à confirmer par un juriste si ce jeu de référence est diffusé ou étendu.
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
| 25/09/2026 | Dev pipeline | O-10.2 (commune OSM de chaque départ, publiée : 877/877) et noms des gares ; O-13 (mode `full` garde les départs publiés, `full_replan`) ; valeurs par défaut du workflow alignées (scénario 1,75/2,5/2,5, 6 durées) ; O-12 : diagnostic `nature_check` v1 à v5 (jeu de 33 boucles de référence), conclusion D21 (pas de changement du score) ; D22 ; O-11 testé OK par Florent. Runs #54-#59 et diagnostics #58 et suivants | O-14 (dev front / UX) ; au prochain `full` : valider O-13 et envisager l'assouplissement des tronçons répétés (D21) |
| 25/09/2026 | Dev pipeline puis dev front / UX | Altitude des départs (`alt_m`) publiée ; run `full` #65 : O-13 validé (877/877 mêmes départs) ; O-14 en ligne (départ voisin qui sort plus vite de la ville, approche montée comprise), vérifié | Test d'O-14 sur le téléphone de Florent ; O-9.5/9.6 (ton, fiche boucle) ; au prochain recalcul complet : assouplissement des tronçons répétés (D21) |
