# HANDOFF — velo-loops (application de parcours vélo pour cyclistes amateurs)

> Dernière mise à jour : 23 septembre 2026
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
- Zone de test : **province de Barcelone et alentours** (~715 départs pré-calculés — confirmé sur le site publié au 22/09/2026 ; le dépôt Git committé n'en montre que 3, voir O-8). Les départs ruraux hors province sont conservés ; le front **ne présente pas** la province comme limite de la zone couverte.
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

### 3.5 Règle de distance au départ (reco UX retenue)
- Distance max à vol d'oiseau : **2 km en zone dense, 3 km en périphérie, 3 km en rural**.
- Affichage en **temps d'approche aller**, pas en km ; au-delà de **30 min → « trop loin »**.

### 3.6 Principes produit non négociables
- Ne promettre que ce qui est **calculable**. Le pitch est **généré à partir de métriques réelles** (ex. « 62 % sur piste cyclable », pas « magnifique route calme » sans donnée).
- Trafic et beauté ne sont pas dans OSM → utiliser des **proxys** (type de route ; forêt, eau, parcs ; densité de photos).
- RGPD : **minimiser** localisation et poids. Mentions claires de responsabilité (parcours à vérifier par le cycliste).

### 3.7 Arborescence du dépôt — confirmée le 23/09/2026
```
velo-loops/
├── .github/workflows/build-loops.yml   # unique workflow (8 modes : plan/detour/pilot/full/site/...)
├── config/                             # région, config GraphHopper, modèles de routage
├── scripts/                            # pipeline Python réel : plan_starts, generate_loops,
│                                        #   measure_detour, fetch_published, report_generation…
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
- [ ] **O-3 — Densifier la couverture** : plus de départs et plus de durées (demande faite à l'expert architecte) ; vérifier la limite de temps/stockage GitHub Actions et Pages.
- [x] **O-4 — Qualité des boucles** — déjà fait, vérifié en O-1 : `scripts/generate_loops.py` rejette les candidats à plus de 25 % de tronçons répétés ou plus de 2 demi-tours, pénalise le score (poids « flow »), et le signale dans le pitch. Reste ouvert : régler/affiner ces seuils si des boucles publiées paraissent encore mauvaises en pratique.
- [x] **O-5 — Pitch fiabilisé** — déjà fait, vérifié en O-1 : `pitch_from_option()` ne produit que des phrases adossées à un champ mesuré exporté (aucune affirmation non vérifiable).
- [x] **O-6 — Feedback après sortie** — déjà fait, vérifié en O-1 : `feedback.js` capture note, minutes réelles, D+ réel, difficulté ressentie, tags +/−, commentaire ; stocké en `localStorage`, exportable en JSON, rien n'est envoyé. Décision « localStorage » prise de fait.
- [ ] **O-7 — Mentions légales / responsabilité** et page RGPD simple. (confirmé absent du front en O-1)
- [x] **O-8 — Nettoyer le dépôt (dette trouvée en O-1)** — fait le 23/09/2026 :
  1. ✅ `web/approach_model.json` resynchronisé avec le site publié (v2.2).
  2. ✅ `scripts/fetch_published.py` retélécharge aussi `web/approach_model.json` en mode `site`, pour empêcher la dérive de se reproduire (fichier toujours géré à la main pour son contenu — voir « reste ouvert » ci-dessous — mais il ne peut plus rester périmé sur le site publié sans que le prochain run `site` ne le corrige).
  3. ✅ Doublon `generate_loops.py` (racine) supprimé.
  4. ✅ `.gitignore` ajouté ; `web/data/` retiré du suivi Git (fichiers gardés sur le disque de Florent, juste plus committés).
  **Reste ouvert, non traité ici** : `web/approach_model.json` est toujours rempli à la main à partir du mode `detour` (aucun script ne transforme `data/detour.json` en `web/approach_model.json`) — la prochaine fois que ce modèle doit être recalculé, il faudra soit l'éditer à la main avec prudence, soit écrire ce script. À évaluer selon la fréquence à laquelle ce recalcul est vraiment nécessaire.

### Backlog des idées retenues (après POC, à prioriser)
Vent/météo pour orienter la boucle · type de séance (endurance, intervalles avec côte 5-6 min, sortie plaisir) · échappatoires (raccourci fatigue/crevaison/orage, points d'eau, boulangeries, gares) · difficulté en langage humain · curseur de fréquentation · carte à dévoiler (zones blanches) · micro-aventure train + vélo (A→B entre deux gares) · guide audio IA des lieux traversés. Détails : `docs/product.md`.

### Questions ouvertes
- Modèle économique (pub produits sport vs abonnement) — à traiter avec un expert business.
- Nom et identité visuelle (outdoor, aventure, découverte, « off the beaten tracks », liberté) — expert branding/graphiste.
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
