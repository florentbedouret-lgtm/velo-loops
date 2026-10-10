# Proposition de contenu pour la v17

Préparée le 09/10/2026, à trancher par Florent. Toutes ces règles sont codées et **éteintes** en production (v16). Chacune
a été mesurée seule, avant / après, sur les mêmes 30 départs (60 pour la marge) ; leurs effets **combinés** ne sont pas
encore mesurés.

**Attention aux premières mesures.** Jusqu'au 09/10 au matin, les diagnostics avant / après tournaient **sans la
retouche** (environ 20 % des boucles recommandées en viennent) : relief, terre, répétition par couloir et les trois
premiers tests de la marge. Leurs conclusions tiennent sur l'essentiel des boucles ; les tests du 09/10 (détours,
retouche à tour de rôle, retours sur ses pas, marge v4) la comprennent.

## Règles

| règle (interrupteur) | ce qu'elle corrige | effet mesuré | avis de Claude |
|---|---|---|---|
| **Répétition par couloir** (`CORRIDOR_OVERLAP`, O-47) | l'autre chaussée, une voie parallèle ou un triangle de carrefour ne comptaient pas comme répétition (Gràcia Modéré 1 h : 4 % mesurés, 47 % réels) | boucles répétées à plus de 25 % : 20 → 1 (Tranquille), 18 → 2, 16 → 1 ; note −0,7 à +0,1 ; Gràcia corrigée | **activer** |
| **Détours en pâté de maisons** (`DETOUR_FIX`, O-46) | points de passage accrochés à la mauvaise chaussée (Sants-Montjuïc 1 h 30 : Paral·lel → Calàbria) | mètres de détour −5 à −18 %, note inchangée ; Sants 1 h 30 : 2 détours → 0 | **activer** (gain modeste, sans effet négatif) |
| **Relief par allure** (`RELIEF_LIMITS_V17_FLAT`, O-45) | Tranquille et Modéré avaient le même relief | Modéré : 14,3 → 12,9 m/km, au-delà de 15 m/km 89 → 57, note −3 ; Tranquille : 13,9 → 9,6 m/km, mais **38 % sans boucle peu vallonnée** (repli annoncé), note −9 | **Modéré : activer. Tranquille : activer (choix A de Florent, 09/10)** : cible 5-10 m/km gardée, repli annoncé (« pas de boucle plate d'ici ») plutôt qu'une boucle de ville |
| **Terre plafonnée à 1 km** (`DIRT_RULES_V17`) | trop de terre dans les boucles recommandées | boucles à plus de 1 km de terre −15 % ; environ 10 % des durées en repli (la moins terreuse, annoncée) ; note −1,4 à −1,9 | **activer** (va dans ton sens, effet limité par le terrain) |
| **Marge des durées** (`DURATION_BIN_MARGIN` 0,05 + filet) | une très bonne boucle de 4 h 24 refusée en « 5 h » | 3e test : 65 boucles meilleures de plus de 3 points (souvent +10 à +30), 4 durées vidées → **filet** ajouté (une durée vide est recalculée en plages strictes) | **activer (accord de Florent, 09/10)** : test v4, retouche comprise : aucune durée perdue, 69 boucles meilleures de plus de 3 points, 7 moins bonnes (toutes de moins de 10 points) |
| **Retours sur ses pas utiles / inutiles** (`BACKTRACK_RULES`, O-51) | un demi-tour au château de Montjuïc coûtait autant qu'un demi-tour absurde | note +2,7 à +3,3 ; 200 boucles meilleures, 4 moins bonnes ; 29 durées vides trouvent une boucle ; Sants 1 h 30 : 49,7 → 58,5 | **activer (accord de Florent, 10/10)**, après audit et relecture de 29 cas par Florent : jamais sur la terre (sauf vers un lieu), « nature » et « piste » sur au moins 1 km, piste seulement vers un endroit agréable, relief seulement avec un demi-tour à un sommet ou col nommé ou au bord de l'eau ; 144 boucles meilleures, 30 moins bonnes (presque toutes la même boucle, dont l'éperon n'est plus excusé), 13 durées vides comblées |
| **Croisement des meilleures boucles** (`CROSSOVER`, O-53) | les morceaux de deux bonnes boucles n'étaient jamais assemblés | 87 boucles recommandées issues d'un croisement sur ~800 ; 13 meilleures de plus de 3 points, **aucune moins bonne** ; note +0,1 à +0,4 | **activer (accord de Florent, 09/10)** ; **avec les boucles des durées voisines** (`CROSS_NEIGHBOURS`, accord de Florent, 09/10) : 54 boucles meilleures, aucune moins bonne, 190 recommandées croisées ; la Plata 1 h 30 52,4 -> 56,7 |
| **Retouche raccourcie** (`RETOUCH_SHORTEN`) | une retouche meilleure mais un peu trop longue était jetée | 5 boucles raccourcies, 2 meilleures, aucune moins bonne ; la Plata inchangée | plus tard (effet faible) |
| **Retouche à tour de rôle** (`RETOUCH_ROUND_ROBIN`, O-52) | la rivière jamais essayée à la Plata | 16 boucles meilleures, 4 moins bonnes ; la Plata **inchangée** | **ne pas activer** pour l'instant (la cause de la Plata est ailleurs : comparaison `cmp3b` en cours) |

## Données (déjà dans les fichiers, effectives au prochain calcul complet)

- Départ « el Tibidabo (est) » retiré (`scripts/start_drops.json`).
- Pantà de Susqueda visé au belvédère de la carretera de Querós.
- Collserola et le Montnegre écartés des lieux remarquables (massifs, pas des points).

## Avant de lancer la v17

1. **Mesurer l'ensemble** (prêt : `diag_part=v1` / `v2`, règles dans `V17_RULES` du workflow, Sants-Montjuïc, Gràcia et
   la Plata ajoutés à v1) : un diagnostic avec toutes les règles retenues allumées en même temps, sur les 30 départs,
   retouche comprise (les règles peuvent se contrarier : par exemple, terre plafonnée et relief Tranquille réduisent
   tous deux le choix).
2. **Activer** : mettre les interrupteurs retenus, les ajouter à l'empreinte (`params_hash`, entrée « v17 ») ; pour le
   relief Tranquille, déplacer `scripts/diag_models/loop_flat.json` dans `config/custom_models/` et ajouter le profil
   `calm_flat` à `config/graphhopper.yml` (le graphe GraphHopper est alors reconstruit) ; mettre à jour le test
   `test_essais_desactives_en_production`.
3. **Calcul complet** (accord de Florent : il publie) ; à la fin, vérifier version 17 et aucun départ `stale`.
