# Proposition de contenu pour la v19

Préparée le 14/10/2026, à trancher par Florent. Toutes ces règles sont codées et **éteintes** en production (v18, calcul
complet en cours). L'empreinte des paramètres (`params_hash`) ne les compte que lorsqu'une d'elles est allumée : les
pousser ne perturbe pas le calcul v18. Chaque règle a été mesurée avant / après (diagnostic `nature_check`, retouche
comprise, boucles publiées remises en jeu comme en production).

## Règles

| règle (interrupteur) | ce qu'elle corrige | effet mesuré | avis de Claude |
|---|---|---|---|
| **Modèle de temps v19** (`TIME_V19`) | durée surestimée en ville vallonnée (Eixample 2 h « plus de relief » : 2 h 01 annoncées, 1 h 48 roulées) ; ralentissement de 25 % sur toute la « ville » de GraphHopper (Vallvidrera comptée comme l'Eixample) | recalé sur les **3 sorties de Florent** ensemble (tranches de 100 m, aucun réglage par sortie) : écart **−1 %, −1 %, 0 %** (avant : jusqu'à +12 %). Puissance **+30 %** à 6 % de pente (≈ 90 % de la FTP, tenable sur de longues montées ; +50 % au 1er réglage : boucles de montagne estimées trop courtes, sorties de leur durée ; choix de Florent, 15/10), descente plafonnée (28 km/h en Modéré, choix de Florent ; 25 km/h mesurés sur lui, prudent), ralentissement selon la **densité de carrefours** (jusqu'à 19 %), 15 s par feu (arrêt + freinage et relance, ajustés ensemble avec la densité : pas de double compte). Diagnostic h1 / h2 (33 départs) : boucles +1 % (Tranquille), +5 % (Modéré), +8 % (Sportif) à durée égale ; 14 durées perdues, 15 gagnées | **activer** |
| **Repli corrigé** (`FALLBACK_BEST`) | sans boucle conforme (relief du Tranquille, terre), le repli gardait LA moins vallonnée quelle que soit sa note (Sant Esteve de Llémena 4 h : 11,9 m/km notée 48,4 au lieu de 12,0 m/km notée 70,8) | la mieux notée à 1 m/km (0,5 km de terre) près : u1 / u2 (33 départs) **57 meilleures, aucune moins bonne**, +4,1 en moyenne sur les 143 durées en repli (Cesalpina 1 h 30 Tranquille 24 → 59) | **activer** |
| **Croisement tronçon par tronçon** (`CROSS_SEGMENTS`) | là où deux bonnes boucles se séparent puis se rejoignent, le meilleur tronçon de chacune | avec le repli corrigé (j1 / j2) : **100 meilleures, 2 moins bonnes** (−2,2 et −0,2), soit 43 de plus que le repli seul ; gains surtout +1 à +5 (Llavaneres 1 h 30 : 20 → 41) ; pas de surcoût de calcul mesurable. Sans le repli corrigé : 3 chutes de −22 (le défaut était le repli) | **activer** avec le repli corrigé |
| **Feux en fin de boucle** (`LIGHTS_LATE`, tolérance 1 point, choix de Florent) | feux au début plus frustrants qu'à la fin (Florent, sortie du 13/10) | e1 / e2 (16 départs urbains) : 13 boucles inversées sur ~190 qui ont leurs feux surtout au début, note −1 au plus, parfois + (Cornellà 1 h +7,5) ; en ville, le sens inverse est presque toujours une autre boucle (sens uniques) | **activer** (jamais nuisible, effet modeste) |
| **Voies agréables** (`SCENIC_RETOUCH`) | une boucle longe une piste goudronnée de bord de mer ou de rivière sans l'emprunter (GraphHopper ne voit pas le paysage) | y1 / y2 (16 départs du littoral) : 21 options en viennent, 18 meilleures, 5 moins bonnes de 0,7 au plus (Martorell 6 h +6,5, Mataró 1 h +5,9, l'Eixample 3 h 55,5 → 57,9 par le Besòs). L'Eixample 2 h par la Barceloneta : la note départage à peine les deux tracés (voir front de mer) | **activer** |
| **Bord d'eau à 80 m en ville** (`WATER_CITY_NEAR`) | le couloir de la B-10, derrière les immeubles, comptait comme bord d'eau | q1 / q2 : notes −0 à −2,4, classement presque inchangé ; rend la note plus honnête (le couloir intérieur ne compte plus comme bord d'eau) | **activer** avec le front de mer (retenu par Florent, 14/10) |
| **Front de mer** (`SEAFRONT_BONUS`) | la promenade de la Barceloneta et le couloir intérieur notaient pareil (53,1 contre 52,8) | l1 / l2 (littoral) : 154 meilleures, 6 moins bonnes (Mataró 5 h Tranquille −7,2, effet de bord de l'ordre des durées) ; 41 % des boucles du littoral en profitent (2,8 km au 9e décile) ; l3 : l'Eixample 1 h 30 Modéré passe par le front de mer (48,8 → 53,1, 5,4 km), mais le 2 h Modéré garde sa boucle : aucune variante par la Barceloneta n'est produite (génération) | **activer** (retenu par Florent, 14/10) ; le 2 h de l'Eixample reste à comprendre |
| **Voies rapides** (`MOTORWAY_NUISANCE`, idée de Florent, v3 : autoroutes seules, `MOTORWAY_TRUNK` = False, tronçons longés sur 300 m au moins) | longer une autoroute à l'air libre (bruit, gaz) | v1 : 95 % des boucles touchées ; v2 (tronçons longés) : 73 % (les « voies express » OSM comprennent la Meridiana, la Gran Via) ; **v3** (i3 / i4, filet des boucles publiées) : 48 % des boucles, médiane 0 km, 1,9 km au 9e décile ; 37 recommandées sur 384 changent ; note −6 au plus (la pénalité, plafonnée à 5) | **activer** (retenu par Florent, 15/10) |
| **Boucles publiées retrouvées à l'identique** (`PREVIOUS_WAYPOINT_M` = 500) | le filet `KEEP_PREVIOUS` recalculait les boucles publiées d'un point tous les 2 km : GraphHopper prenait un autre chemin, et une bonne boucle publiée pouvait disparaître (Sant Martí 3 h Tranquille publiée à 72,2, republiée à 60,3 par la v18) | a1 / a2 (34 départs, 1 144 boucles) : retrouvées à l'identique 73 % (2 km) → **95 % (500 m)** ; identiques et valides 69 % → 89 % ; retouchées 118 → 150 sur 162 (300 m : plus de demi-tours, moins bien). m4 à m6 : Mataró 5 h et Sant Martí 3 h sauvés ; Gràcia 1 h 30 non (sa meilleure boucle n'a jamais été publiée) | **activer** |
| **Reclassement des boucles publiées** (`PREVIOUS_ANY_DURATION`) | avec un nouveau modèle de temps, une très bonne boucle sort de la plage de sa durée et n'est plus proposée nulle part (mesure combinée : 41 durées à −10 points et plus) | traçages a3 / a4 : la boucle est reprise à sa nouvelle durée (Sant Cugat 1 h 30 Sportif 74,2 → 84,1) ; la durée quittée reste à remplir (d'où le bonus en montée ramené à +30 %) | **activer** (Florent, 15/10) |
| **« Plus de relief » du Modéré** (`PLUS_RELIEF_RULES`) | la pénalité au-dessus de 15 m/km écartait les boucles vallonnées de l'option faite pour elles | q3 / q4 : 9 durées gagnent une option « plus de relief », 5 perdent la leur (interdits) ; prendre la plus vallonnée faisait baisser la note (l'Eixample 2 h 46,5 → 41,2) : on prend désormais **la mieux notée** des nettement plus vallonnées (+3 m/km) | **activer** (Florent : 20 m/km, la mieux notée) |
| **Contourner les grands axes** (`AXIS_BYPASS`) | aucune recherche n'essayait d'éviter un tronçon de grand axe par les rues voisines (Gràcia 2 h, idée de Florent) | a7 / a8 (16 départs) : 36 meilleures, 5 moins bonnes (−1,2 au plus), jusqu'à +6,9 ; le raccourci de Gràcia n'est pas attrapé (critère « en ville » à affiner) | **activer** (Florent, 15/10) |
| **Voies agréables, points serrés** (`SCENIC_DENSE`) | trois points sans sens : la variante faisait des allers-retours | Eixample 2 h : 35,3 → 49,2 (encore sous la boucle publiée) ; portions de 2 km (`SCENIC_WINDOWS`) : pires, non retenues | **activer** avec les voies agréables |

**Boucles perdues d'un calcul à l'autre (O-63, cause trouvée)** : ce n'était pas l'ordre des durées (traçage m1 à m3 : les bornes n'écartent rien). Deux causes : (1) le filet des boucles publiées ne les retrouvait pas toujours (corrigé ci-dessus) ; (2) la retouche et les croisements partent des meilleures boucles du moment : quand une règle change les notes, leurs meilleurs résultats peuvent ne plus être produits (recherche locale). La (2) reste, mais ne peut plus effacer une boucle publiée.

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
3. `python scripts/apply_v19.py .` (prêt, testé sur une copie le 15/10) : allumer les interrupteurs retenus, `GENERATOR_VERSION = "19"`, mettre à jour `tests/test_rules.py` (interrupteurs) ;
   l'empreinte compte alors la v19 (`V19_PARAMS`) : calcul complet (`mode=full`, `reuse=true`, `chain`).
4. Publier les textes de l'app (section suivante) avec le site de la v19.
5. À la fin : version 19 partout, aucun `stale` ; vérifier les durées annoncées sur les départs des sorties de Florent.

## Textes de l'app à publier avec la v19 (validés par Florent le 14/10/2026)

Les textes actuels restent justes pour la v18 : ne les changer qu'au lancement de la v19.

1. `notes` (`i18n.js`, sous chaque boucle) — « en mouvement » était trompeur (les arrêts aux feux sont comptés) :
   - FR : Durées estimées sans les pauses, arrêts aux feux compris. Dénivelé : estimation.
   - EN : Estimated times without breaks, stops at traffic lights included. Climbing is an estimate.
   - ES : Tiempos estimados sin pausas, paradas en semáforos incluidas. Desnivel: estimación.
   - CA : Temps estimats sense pauses, aturades als semàfors incloses. Desnivell: estimació.
2. `how_4` (`i18n.js`) et la question « Comment la durée est-elle estimée ? » des quatre pages « Comment ça marche »
   (texte visible et FAQ en JSON-LD) ; seul le début change, la suite sur la FTP reste :
   - FR : La durée est estimée pour ton allure (tranquille, modéré, sportif) : un peu plus d'effort dans les montées, des descentes prudentes, un ralentissement dans les quartiers denses, et environ 15 s par feu (arrêt, freinage et relance). Les pauses ne sont pas comptées. Ce calcul a été recalé sur des sorties réelles.
   - EN : The time is estimated for your pace (easy, moderate, sporty): a little more effort on climbs, careful descents, slower riding in dense neighbourhoods, and about 15 s per traffic light (stopping, braking and getting going again). Breaks are not counted. The estimate was calibrated on real rides.
   - ES : La duración se estima para tu ritmo (tranquilo, moderado, deportivo): algo más de esfuerzo en las subidas, bajadas prudentes, más lento en los barrios densos y unos 15 s por semáforo (parada, frenada y arranque). Las pausas no se cuentan. El cálculo se ha ajustado con salidas reales.
   - CA : La durada es calcula per al teu ritme (tranquil, moderat, esportiu): una mica més d'esforç a les pujades, baixades prudents, més lent als barris densos i uns 15 s per semàfor (aturada, frenada i arrencada). Les pauses no es compten. El càlcul s'ha ajustat amb sortides reals.
