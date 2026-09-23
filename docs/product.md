# Vision produit — référence

## Problème
Un cycliste amateur veut s'entraîner sur un parcours agréable adapté à son temps et à son niveau, sans passer du temps à tracer une route ni subir la ville, les feux et le trafic.

## Parcours utilisateur cible

### Avant la sortie
- Entrées : point de départ, durée (heures), niveau de difficulté ; éventuellement type de route et plat vs dénivelé.
- Sortie : plusieurs options optimisées pour l'agrément (éviter ville, feux, zones de pollution ; privilégier pistes cyclables et paysages).
- Pour chaque option : carte, km, dénivelé, % en ville, montées, **pitch** expliquant les choix à partir de métriques réelles.
- Option future : importer une sortie difficile récente pour jauger le niveau.

### Pendant
- Pas de GPS intégré : **export GPX** vers Garmin, Wahoo, Apple Watch, Strava.

### Après
- Feedback : note, difficulté réelle, temps réel, points positifs/négatifs.
- Usage : éviter/pousser certains éléments, réévaluer difficulté et temps estimés pour l'utilisateur (règles simples, pas de ML au départ).

### Difficulté
- Vélo + poids → watts estimés en **calcul interne** uniquement ; l'utilisateur voit une difficulté en langage humain.

## Idées retenues après brainstorming (backlog post-POC)
- **Vent/météo** : vent de face à l'aller, dans le dos au retour.
- **Type de séance** : endurance 1 h, intervalles avec une côte de 5-6 min, sortie plaisir.
- **Échappatoires** : raccourci en cas de fatigue, crevaison, orage ; points d'eau, boulangeries, gares.
- **Difficulté expliquée** en langage humain pour les débutants.
- **Curseur de fréquentation** : une route isolée n'est pas rassurante pour tous (ex. femmes seules, tôt ou tard).
- Idées audacieuses : **carte à dévoiler** (zones blanches à explorer) ; **micro-aventure train + vélo** (A→B entre deux gares) ; **guide audio IA** racontant les lieux traversés.

## Plus tard
- Social : partenaires d'entraînement de niveau similaire selon disponibilités.
- Multi-sport : course, trail, VTT.
- Modèle économique : gratuit avec pub (produits sport) ou abonnement mensuel avec options — non tranché.

## Risques identifiés
| Risque | Parade |
|---|---|
| Trafic et beauté absents d'OSM | Proxys (type de route ; forêt, eau, parcs ; densité de photos) ; pitch limité au calculable |
| Accès API Strava/Garmin restreint | Démarrer par GPX + feedback manuel ; revérifier les conditions Strava |
| Boucles de mauvaise qualité (demi-tours, allers-retours) | Détection et pénalisation dans le pré-calcul |
| RGPD | Minimiser localisation et poids |
| Responsabilité (parcours dangereux) | Mentions claires |

## Identité (à travailler)
Nom et visuel orientés : sport outdoor, aventure, découverte, « off the beaten tracks », liberté.
