# Proposition de contenu pour la v19

Préparée le 14/10/2026, à trancher par Florent. Toutes ces règles sont codées et **éteintes** en production (v18, calcul
complet en cours). L'empreinte des paramètres (`params_hash`) ne les compte que lorsqu'une d'elles est allumée : les
pousser ne perturbe pas le calcul v18. Chaque règle a été mesurée avant / après (diagnostic `nature_check`, retouche
comprise, boucles publiées remises en jeu comme en production).

## Règles

| règle (interrupteur) | ce qu'elle corrige | effet mesuré | avis de Claude |
|---|---|---|---|
| **Modèle de temps v19** (`TIME_V19`) | durée surestimée en ville vallonnée (Eixample 2 h « plus de relief » : 2 h 01 annoncées, 1 h 48 roulées) ; ralentissement de 25 % sur toute la « ville » de GraphHopper (Vallvidrera comptée comme l'Eixample) | recalé sur les **3 sorties de Florent** ensemble (tranches de 100 m, aucun réglage par sortie) : écart −1 %, −2 %, −3 % (avant : jusqu'à +12 %). Puissance +50 % à 6 % de pente, descente plafonnée (28 km/h en Modéré, choix de Florent ; 25 km/h mesurés sur lui, prudent), ralentissement selon la **densité de carrefours** (jusqu'à 19 %), 15 s par feu (arrêt + freinage et relance, ajustés ensemble avec la densité : pas de double compte). Diagnostic h1 / h2 (33 départs) : boucles +1 % (Tranquille), +5 % (Modéré), +8 % (Sportif) à durée égale ; 14 durées perdues, 15 gagnées | **activer** |
| **Repli corrigé** (`FALLBACK_BEST`) | sans boucle conforme (relief du Tranquille, terre), le repli gardait LA moins vallonnée quelle que soit sa note (Sant Esteve de Llémena 4 h : 11,9 m/km notée 48,4 au lieu de 12,0 m/km notée 70,8) | la mieux notée à 1 m/km (0,5 km de terre) près : u1 / u2 (33 départs) **57 meilleures, aucune moins bonne**, +4,1 en moyenne sur les 143 durées en repli (Cesalpina 1 h 30 Tranquille 24 → 59) | **activer** |
| **Croisement tronçon par tronçon** (`CROSS_SEGMENTS`) | là où deux bonnes boucles se séparent puis se rejoignent, le meilleur tronçon de chacune | avec le repli corrigé (j1 / j2) : **100 meilleures, 2 moins bonnes** (−2,2 et −0,2), soit 43 de plus que le repli seul ; gains surtout +1 à +5 (Llavaneres 1 h 30 : 20 → 41) ; pas de surcoût de calcul mesurable. Sans le repli corrigé : 3 chutes de −22 (le défaut était le repli) | **activer** avec le repli corrigé |
| **Feux en fin de boucle** (`LIGHTS_LATE`, tolérance 1 point, choix de Florent) | feux au début plus frustrants qu'à la fin (Florent, sortie du 13/10) | e1 / e2 (16 départs urbains) : 13 boucles inversées sur ~190 qui ont leurs feux surtout au début, note −1 au plus, parfois + (Cornellà 1 h +7,5) ; en ville, le sens inverse est presque toujours une autre boucle (sens uniques) | **activer** (jamais nuisible, effet modeste) |
| **Voies agréables** (`SCENIC_RETOUCH`) | une boucle longe une piste goudronnée de bord de mer ou de rivière sans l'emprunter (GraphHopper ne voit pas le paysage) | y1 / y2 (16 départs du littoral) : 21 options en viennent, 18 meilleures, 5 moins bonnes de 0,7 au plus (Martorell 6 h +6,5, Mataró 1 h +5,9, l'Eixample 3 h 55,5 → 57,9 par le Besòs). L'Eixample 2 h par la Barceloneta : la note départage à peine les deux tracés (voir front de mer) | **activer** |
| **Bord d'eau à 80 m en ville** (`WATER_CITY_NEAR`) | le couloir de la B-10, derrière les immeubles, comptait comme bord d'eau | q1 / q2 : notes −0 à −2,4, classement presque inchangé ; rend la note plus honnête (le couloir intérieur ne compte plus comme bord d'eau) | **activer** avec le front de mer (retenu par Florent, 14/10) |
| **Front de mer** (`SEAFRONT_BONUS`) | la promenade de la Barceloneta et le couloir intérieur notaient pareil (53,1 contre 52,8) | l1 / l2 (littoral) : 154 meilleures, 6 moins bonnes (Mataró 5 h Tranquille −7,2, effet de bord de l'ordre des durées) ; 41 % des boucles du littoral en profitent (2,8 km au 9e décile) ; l3 : l'Eixample 1 h 30 Modéré passe par le front de mer (48,8 → 53,1, 5,4 km), mais le 2 h Modéré garde sa boucle : aucune variante par la Barceloneta n'est produite (génération) | **activer** (retenu par Florent, 14/10) ; le 2 h de l'Eixample reste à comprendre |
| **Voies rapides** (`MOTORWAY_NUISANCE`) | longer une autoroute ou une voie express à l'air libre (bruit, gaz) | i1 / i2 : **95 % des boucles touchées** (1,4 km en médiane) : chaque passage dessus ou dessous compte ; 362 moins bonnes, jusqu'à −20 (Sant Martí 3 h Tranquille, effet de bord de l'ordre des durées) | **ne pas activer** ; à refaire en ne comptant que les tronçons longés sur au moins 300 m |

**Effet de bord de l'ordre des durées** (vu avec les départs voisins, le front de mer et les voies rapides) : une note qui change sur une durée décale les bornes des durées voisines, et une durée peut perdre sa meilleure boucle (Sant Martí 3 h Tranquille 72,2 → 52,2). À étudier avant la v19 : toute règle de note peut le déclencher.

Non retenues : zones agréables pour GraphHopper (`SCENIC_AREAS`, bonus uniforme : chutes de −25 à −29) ; croisement
avec les départs voisins (`CROSS_SPATIAL` : Gràcia 2 h inchangée, baisses à expliquer — effet de bord de l'ordre des
durées en Modéré, à étudier). **Les départs voisins restent à approfondir** (Florent, 14/10 : « un vrai potentiel
d'amélioration ») : 152 boucles meilleures, des gains forts (les Corts 1 h 30 Tranquille 24 → 49, Badalona 2 h 78) ;
comprendre pourquoi la boucle de l'Eixample par le coll del Portell n'est pas reprise pour Gràcia, corriger l'ordre des
durées (O-63), puis remesurer.

## Activer la v19

1. Attendre la fin du calcul v18 (aucun départ `stale`).
2. Mesurer l'ensemble retenu en une fois (comme la mesure combinée de la v17) : les règles peuvent se contrarier
   (temps v19 et voies agréables changent toutes deux les durées).
3. Allumer les interrupteurs retenus, `GENERATOR_VERSION = "19"`, mettre à jour `tests/test_rules.py` (interrupteurs) ;
   l'empreinte compte alors la v19 (`V19_PARAMS`) : calcul complet (`mode=full`, `reuse=true`, `chain`).
4. À la fin : version 19 partout, aucun `stale` ; vérifier les durées annoncées sur les départs des sorties de Florent.
