# HANDOFF — velo-loops (application de parcours vélo pour cyclistes amateurs)

> Dernière mise à jour : 23 septembre 2026
> Porteur du projet : Florent (débutant en code, travaille en « vibe coding » avec Claude)
> À lire au début de chaque session, avec `rules.md`. Détails produit : `docs/product.md`. Journal des décisions : `docs/decisions.md`.

⚠️ **Limite de ce document** : il a été reconstitué à partir des notes de synthèse des conversations précédentes, pas du code. Les éléments marqués **[À VÉRIFIER]** (arborescence exacte, formats de fichiers, noms de workflows) doivent être confirmés en lisant le dépôt lors de la première session (voir tâche O-1).

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
- Zone de test : **province de Barcelone et alentours** (~715 départs pré-calculés). Les départs ruraux hors province sont conservés ; le front **ne présente pas** la province comme limite de la zone couverte.
- **Boucles pré-calculées** (départ pré-calculé le plus proche + durées à choix). Le routage à la demande par serveur est **repoussé**.
- **Pas de GPS intégré** : export **GPX** vers Garmin, Wahoo, Apple Watch, Strava.
- **Watts** : calcul interne uniquement pour estimer la difficulté, jamais affichés.
- **Personnalisation** : 3-4 règles simples, **pas de machine learning** au départ.
- **Repoussés** : social, publicité, multi-sport, app native, test utilisateur formel.

### 3.3 Stack technique (0 €)
| Élément | Choix |
|---|---|
| Hébergement code | Dépôt GitHub **public** `velo-loops` (compte GitHub créé pour le projet) — **[À VÉRIFIER] nom du compte / URL** |
| Données | OpenStreetMap (gratuit, open source) |
| Routage | GraphHopper, exécuté dans **GitHub Actions** pour pré-calculer les boucles |
| Stockage des boucles | Fichiers statiques dans le dépôt (**[À VÉRIFIER] format : GeoJSON/JSON/GPX, dossier**) |
| Front | Web app statique sur **GitHub Pages** (= version de travail du POC) |
| Géocodage | **Photon** (géocodeur public de Komoot) pour la recherche d'adresse |
| Localisation | API de géolocalisation du navigateur |

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

### 3.7 Arborescence du dépôt — [À VÉRIFIER, à compléter en O-1]
```
velo-loops/
├── .github/workflows/      # pré-calcul des boucles (OSM + GraphHopper)
├── <données boucles>/      # fichiers statiques générés
├── <front>/                # web app servie par GitHub Pages (HTML/JS/CSS)
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

### État au 23/09/2026
Le POC tourne en ligne : recherche d'adresse + géolocalisation + départ le plus proche + boucles + GPX. Florent modifie parfois le code directement sur GitHub → **toujours `git pull` avant de travailler**.

### Tâches
- [ ] **O-1 — Audit du dépôt (priorité 1, première session Claude Code)**
  Cloner/puller `velo-loops`, lire le code et compléter ici la section 3.3/3.7 : arborescence réelle, format des fichiers de boucles, nom et déclencheur du workflow GitHub Actions, profil GraphHopper utilisé, niveaux de difficulté et durées proposés. Supprimer les mentions [À VÉRIFIER] une fois confirmées.
- [ ] **O-2 — Appliquer la règle dmax/temps d'approche (3.5)** si elle n'est pas encore codée : classifier dense/périphérie/rural, afficher le temps d'approche, message « trop loin » > 30 min.
- [ ] **O-3 — Densifier la couverture** : plus de départs et plus de durées (demande faite à l'expert architecte) ; vérifier la limite de temps/stockage GitHub Actions et Pages.
- [ ] **O-4 — Qualité des boucles** : détecter et pénaliser demi-tours, allers-retours absurdes, recouvrement aller/retour.
- [ ] **O-5 — Fiabiliser le pitch** : chaque phrase liée à une métrique calculée (% piste cyclable, % en ville, dénivelé, proxys paysage).
- [ ] **O-6 — Feedback après sortie (manuel)** : note, difficulté ressentie, temps réel, +/− ; stockage minimal (localStorage au POC ?) — décision à prendre.
- [ ] **O-7 — Mentions légales / responsabilité** et page RGPD simple.

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
