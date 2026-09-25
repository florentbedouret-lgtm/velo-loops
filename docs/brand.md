# Plateforme de marque — v1

> Validée le 23 septembre 2026 avec Florent (rôle : directeur artistique / expert branding).
> Référence pour toutes les étapes suivantes : nom, couleurs, typographies, logo, ton des textes de l'app.

## 1. Cible

**Cycliste amateur urbain, 25-40 ans**, sportif polyvalent (triathlon, marathon), classe moyenne ou supérieure, roule souvent seul. Il roule pour le plaisir d'être dehors, pour se déconnecter de la ville et pour se dépasser.

**Personas**
- **Clara, 33 ans** : consultante, prépare un half-Ironman, dispose d'1 h 30 le samedi matin.
- **Julien, 29 ans** : développeur, finisher de marathon, s'ennuie sur ses boucles habituelles.

## 2. Insight

« Je vis en ville et j'ai peu de temps. Je veux en sortir vite, retrouver la nature et me déconnecter, et découvrir autre chose que mes routes habituelles, sans passer une heure à tracer un parcours. »

## 3. Promesse

**Des boucles neuves vers la nature, loin de la ville, prêtes en quelques secondes et expliquées avec des chiffres réels.**

## 4. Mission

Rendre à chaque sortie le goût de l'inconnu.

## 5. Valeurs

| Valeur | Ce que ça veut dire |
|---|---|
| **Exploration** | Il y a toujours une route que tu ne connais pas. |
| **Honnêteté** | On ne promet que ce qu'on mesure (principe 3.6 du handoff). |
| **Simplicité** | Départ, durée, niveau, et c'est parti. |

## 6. Le dépassement, version « découverte »

L'utilisateur veut se dépasser, mais la marque reste sur son positionnement découverte (décision D1). Le dépassement s'exprime donc comme :
- **aller plus loin** : une route jamais faite, une côte nouvelle, une boucle plus longue ;
- **un défi choisi** : le niveau de difficulté que l'utilisateur sélectionne ;
- **jamais** comme des chiffres de performance (allures, watts, classements).

Exemple : « Une côte de 4 km que tu n'as jamais montée », plutôt que « Explose ton record ».

## 7. Personnalité

**Un éclaireur local** : curieux, précis, sobre, un brin audacieux.
Ce n'est ni un coach qui crie, ni un guide touristique bavard.

## 8. Ton

Tutoiement, phrases courtes, chiffres concrets, vocabulaire de l'extérieur (air, forêt, horizon) plutôt que de la souffrance.

- ✅ « 62 % sur piste cyclable, sortie de ville en 9 min. »
- ✅ « 38 % du parcours en forêt ou au bord de l'eau. »
- ❌ « Un parcours magique et inoubliable ! » (non mesurable)
- ❌ « No pain, no gain. » (beast mode)

## 9. Direction esthétique

**Sobre et élégant, avec une élégance naturelle** : beaucoup d'espace blanc, une palette inspirée du terrain (terre, pierre, ciel, végétation), un seul accent de couleur, des typographies fines et lisibles.

**À éviter** : l'esthétique « beast mode » (noir et rouge, typographies agressives), le lycra en pleine souffrance, et à l'inverse un luxe froid qui ferait oublier l'extérieur.

## 10. Positionnement face à la concurrence

- **Komoot** : très riche, mais demande de planifier.
- **Strava** : centré sur la performance et le social, propose aussi des suggestions de parcours.
- **Nous** : zéro effort de planification, sortie de ville rapide, nature mesurée, pitch chiffré.

L'analyse détaillée de la concurrence relève de l'expert marketing.

## 11. Points de vigilance

- **Nature et paysages** : ce n'est pas dans OSM. Toute promesse de « nature » doit reposer sur un proxy calculé (% forêt, eau, parcs). Sinon, on ne l'écrit pas.
- **Genre** : la marque doit parler autant à Clara qu'à Julien.
- **Biais du fondateur** : la cible ressemble au porteur du projet ; à confronter à d'autres cyclistes quand ce sera possible.

## 12. Nom

**Oyan** (décision D18). Remplace le nom de travail « velo-loops », qui reste le nom du dépôt.

⚠️ Disponibilité non vérifiée (marque INPI/EUIPO, nom de domaine) : à confier à un juriste en propriété intellectuelle avant d'investir davantage dans le nom.

## 13. Identité visuelle — v1

> Validée par étapes avec Florent dans Claude Design (septembre 2026), décision D19. La charte complète (`Oyan Charte`), les exports SVG/PNG et `oyan-tokens.css` sont dans le lot exporté de Claude Design, **pas encore versés dans le dépôt**.

### 13.1 Moodboard (étape 1)
Trois lignes directrices : **tracé signature**, **petit cycliste / grand horizon**, **lumière rasante et palette terrain**. La couleur du tracé est l'accent unique.

Les images du moodboard sont des références aux **droits non vérifiés** : jamais dans le dépôt public ni dans l'app (placeholders à la place).

### 13.2 Couleurs — direction « Argile » (étape 2)

| Nom | Code | Usage |
|---|---|---|
| **Argile** | `#A8522F` | Tracé des boucles, bouton principal, point du logo. **Seule couleur d'accent.** |
| Basalte | `#24221F` | Texte principal, anneau du logo |
| Galet | `#625D55` | Texte secondaire, libellés |
| Chaux | `#F6F4F0` | Fond de page, fond de l'icône |
| Surface | `#FFFFFF` | Cartes, feuilles, liseré du tracé |
| Filet | `#E6E2DB` | Séparateurs, bordures |

**Terrain** (barre de répartition uniquement, jamais pour du texte ni un bouton) : ville `#3A3F3B`, forêt `#6E7A5A`, eau `#8FA3AB`, campagne `#D4C3A3`.

Contrastes WCAG vérifiés dans la charte : basalte/surface 15,9:1, galet/chaux 5,9:1, blanc/argile 5,4:1 (tous ≥ AA).

### 13.3 Typographies (étape 2)
- **Hanken Grotesk** 300/400 (500 ponctuel) : textes et chiffres. Chiffres mesurés en Light (ex. distance en 300 · 48 px).
- **IBM Plex Mono** 400 : libellés en capitales, 10 px, espacement 0,12 em.
- Licence OFL (gratuite). À **héberger dans le dépôt** plutôt que charger depuis Google Fonts (évite d'envoyer l'IP des visiteurs à Google, règle 13).

### 13.4 Logo (étape 3)
Anneau ouvert à 45° en haut à droite, trait 5,5 à bouts arrondis (grille 48), avec un **point argile** au départ de l'ouverture, suivi de « yan » en Hanken Grotesk 400.
- Zone de protection : hauteur du point (9 unités) tout autour.
- Taille minimale : logo complet 16 px de haut ; signe seul 12 px.
- Déclinaisons : couleur, sur basalte, inversé, noir, blanc.
- L'idée « route vers l'horizon » (3b) est réservée à l'illustration.

### 13.5 Icône et favicon (étape 4)
Fond chaux, anneau basalte, point argile. Signe à 60 % du carré pour l'icône d'app ; à 80 % avec trait 7 pour le favicon.

### 13.6 Règles d'usage
**À faire** : argile réservée au tracé, au bouton principal et au point du logo ; tracé toujours avec liseré blanc sur la carte ; chiffres réels en Hanken Light, libellés en mono capitales ; photos larges, cycliste petit, de dos ou de loin, femmes et hommes, personnes blanches et non blanches ; une information principale par écran.

**À éviter** : déformer, pivoter, fermer l'anneau ou déplacer l'ouverture ; une 2ᵉ couleur d'accent, ou du vert/bleu pour le tracé ; noir pur et rouge, typos grasses ; chiffres de performance ; imagerie de souffrance ; vélo ou roue dans le logo.

### 13.7 Points de vigilance (avis DA, 25/09/2026)
- **Lecture « Cyan »** : l'anneau ouvert suivi de « yan » peut se lire « Cyan ». À tester à 16 px (onglet du navigateur) et auprès de personnes qui ne connaissent pas le nom.
- **Tracé argile sur la carte réelle** : le fond de carte a déjà des routes orange/jaunes ; contraste à vérifier à l'écran. Les boucles non sélectionnées (aujourd'hui gris-bleu, interdit par la charte) devront passer sur un neutre.
- **Barre de terrain** = promesse de données : ne l'afficher que si le pipeline calcule réellement ces pourcentages (à vérifier par l'expert dev).

## Suite

- Étape 1 : plateforme de marque ✅
- Étape 2 : nom ✅ (Oyan)
- Étape 3 : identité visuelle ✅ (v1 ci-dessus)
- Prochaines étapes : verser les exports dans le dépôt (sans le moodboard) ; appliquer la charte à l'app ; ton des textes de l'app (pitch, boutons) ; vérification juridique du nom.
