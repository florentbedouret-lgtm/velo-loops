# Proposition pour la v20 : des boucles construites autour d'un but

Préparée le 22/10/2026, à discuter avec Florent avant tout code. Point de départ : son critère (D81), la boucle que
ferait un cycliste omniscient qui connaît toutes les routes de sa région, de ce niveau, partant de ce point pour cette
durée ; et ses 43 jugements à l'aveugle des 21 et 22/10 (revue de 23 boucles, comparaison de 20 paires).

## Ce que disent ses jugements

| constat | chiffres |
|---|---|
| Il choisit presque toujours la version qui **repasse le moins** | 11 paires sur 12 ; ses « je la ferais » repassent sur moins de 5 % (2 % en moyenne), ses « bof » et « non » sur 16-17 % |
| Il préfère, à sortie égale, celle qui **tourne le moins** | 8 paires sur 12 (« des directions simples ») |
| Il aime les **petites routes de campagne ou de forêt goudronnées** et la nature | ses « je la ferais » : 84 % de nature et de campagne, 16 % de ville ; les autres : 56-59 % et 41-44 % |
| Il refuse les **allers-retours sans but** et les **détours absurdes** | 1er reproche de la revue (10 boucles sur 23) ; « ce genre de bug décrédibiliserait l'application » |
| Il accepte un **retour par le même chemin s'il est le seul agréable** | « je préfère revenir le long de la mer plutôt que par une route différente mais pas agréable » |
| La note actuelle d'Oyan **ne prédit pas** son jugement | Ripoll 83,5 (la meilleure note de l'échantillon) : « bof », 90 % répétés |

## Pourquoi la v19 n'y arrive pas

Toutes les façons de tirer des boucles (round_trip de GraphHopper, triangles vers un lieu, tour d'un lieu, aller-retour
vers la mer ou une rivière, croisements, greffes de voisins) donnent **quelques points de passage** à GraphHopper et
prennent le chemin qu'il préfère entre eux. Trois conséquences :

1. **Le retour reprend souvent l'aller** : rien n'empêche GraphHopper de revenir par le même couloir s'il est le plus
   rapide, d'où la répétition, que la note ne pèse presque pas (25 % répétés : 4,5 points).
2. **Le profil vélo cherche les pistes cyclables**, urbaines le plus souvent, plutôt que les petites routes de campagne ;
   il fait même le tour d'un pâté de maisons pour une piste (détours que le nettoyage ne sait pas retirer).
3. **Les défauts sont corrigés après coup** (pénalités, nettoyage) : chaque correction déplace le problème et il en faut
   une nouvelle ; on a empilé une quarantaine de règles depuis la v16.

## Ce que je propose

### 1. Partir d'un but, comme le cycliste

Pour chaque départ, allure et durée, lister les **buts** à la bonne distance (environ 40 % du parcours à vol d'oiseau
pour une boucle, davantage pour un aller-retour) : cols et sommets atteignables à l'allure, belvédères, lieux
remarquables, villages, forêts, rivières (à suivre), mer et lacs (à longer sur une partie si possible), et rien
d'autre (Florent). Les classer par intérêt. Une boucle = **aller au but par un
chemin agréable, revenir par un autre chemin agréable**. Les boucles sans but (simple tour par de belles routes) restent
possibles : le but est alors une zone agréable (une forêt, une vallée).

### 2. Un retour vraiment différent

Calculer l'aller, puis le retour **en demandant à GraphHopper d'éviter le couloir de l'aller** (zone tampon de ~100 m
pénalisée dans un modèle personnalisé de la requête, sans interdiction : si le seul retour agréable est le même chemin,
il le reprendra, comme le veut Florent pour la mer). La répétition baisse à la source au lieu d'être pénalisée après.

### 3. Un profil vélo à son goût

Modèle personnalisé par requête (déjà utilisé pour les montées) : **petites routes goudronnées hors de la ville**
privilégiées (unclassified, tertiary), grands axes et voies rapides évités, pistes cyclables privilégiées surtout là
où elles sont agréables (bord de l'eau, parc), pas pour elles-mêmes en ville ; pas de tour de pâté de maisons pour
gagner 100 m de piste.

### 4. Une note de « naturel » étalonnée sur ses jugements

Remplacer l'empilement de pénalités par une note courte, vérifiée sur les 43 jugements (et ceux à venir) : répétition
(fortement), allers-retours sans but et détours (presque éliminatoires), virages par km, part de petites routes et de
nature, grands axes et voies rapides, relief selon l'allure. Règle de validation : elle doit classer les « je la
ferais » devant les « bof » et les « non », et retrouver ses choix dans les paires.

### 5. Une revue à l'aveugle à chaque version

La page de revue (verdict et commentaire) et la comparaison A / B sur la carte deviennent le contrôle de chaque
version : un échantillon tiré au hasard, jugé par Florent, avant toute publication.

## Ordre proposé

1. Retour qui évite l'aller (2) et profil vélo (3) : les plus simples, mesurables tout de suite sur les 23 départs de
   la revue, sans rien changer d'autre.
2. Note de « naturel » (4) en parallèle, à partir des mesures existantes.
3. Construction autour d'un but (1) : le plus gros morceau, en remplacement progressif des tirages au hasard.
4. Revue à l'aveugle (5) avant de lancer la v20.

## Réponses de Florent (22/10/2026)

- **Buts** : col, belvédère, village, lieu remarquable, forêt, rivière (en la suivant), mer et lac (en les longeant sur
  une partie si possible). Rien d'autre.
- **Retour par le même chemin** : même raisonnement en forêt et en montagne qu'au bord de l'eau ; un aller-retour
  agréable vaut mieux qu'un retour désagréable.
- **Hasard** : oui, garder une part de tirages au hasard pour la surprise.
