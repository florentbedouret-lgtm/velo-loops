# Journal des décisions

Format : une ligne par décision. Ajouter en bas ; ne pas effacer — si une décision change, ajouter une nouvelle ligne qui la remplace.

| # | Date | Décision | Raison | Expert |
|---|---|---|---|---|
| D1 | sept. 2026 | Positionnement « découverte », pas programme d'entraînement | Différenciation, périmètre maîtrisable | Business design |
| D2 | sept. 2026 | Pas de GPS intégré, export GPX | Les cyclistes ont déjà montre/compteur ; énorme gain de complexité | Produit / dev |
| D3 | sept. 2026 | Watts en calcul interne uniquement | Estimer la difficulté sans promettre une précision non tenable | Produit |
| D4 | sept. 2026 | Personnalisation par 3-4 règles, pas de ML | Pas de données au départ | Dev |
| D5 | sept. 2026 | Social, pub, multi-sport repoussés | Focus POC | Business |
| D6 | sept. 2026 | Lancement : France seule ; web app avant natif | Focus, coût | Business / dev |
| D7 | sept. 2026 | Zone de test : province de Barcelone | Florent y vit et peut juger les parcours | Produit |
| D8 | sept. 2026 | Coût zéro, données ouvertes (OSM) | Contrainte du porteur | Architecte |
| D9 | sept. 2026 | Boucles pré-calculées par GitHub Actions (OSM + GraphHopper), fichiers statiques, front sur GitHub Pages, dépôt public `velo-loops` | Gratuit, sans serveur | Architecte |
| D10 | sept. 2026 | Historique de nouveauté construit de zéro, sans import Strava | Simplicité, API restreintes | Produit |
| D11 | sept. 2026 | Routage à la demande repoussé ; départ pré-calculé le plus proche + durées à choix | Coût zéro | Architecte |
| D12 | sept. 2026 | Pas de test utilisateur formel pour l'instant | Choix du porteur (à reprendre plus tard) | — |
| D13 | sept. 2026 | Dmax au départ : 2 km dense / 3 km périphérie / 3 km rural ; affichage en temps d'approche, > 30 min = « trop loin » | Lisibilité pour l'utilisateur | UX |
| D14 | sept. 2026 | Site GitHub Pages = version de travail ; départs hors province conservés ; ne pas afficher la province comme limite | Couverture perçue | Produit / UX |
| D15 | sept. 2026 | Géocodage via Photon (Komoot) | Gratuit, sans clé | Dev front-end |
| D16 | 23/09/2026 | Dmax au départ : 1,75 km dense / 2,5 km périphérie / 2,5 km rural (remplace D13 : 2/3/3 km) | Densification O-3 : mesuré, tient dans le budget Actions (64 min/300 min) et améliore le pire cas rural (P90 temps d'approche 35,35 → 29,04 min) | Architecte |
| D17 | 23/09/2026 | Pas de pourcentage de pente affiché (catégories qualitatives seulement) ; badge « rampe très raide » gardé au seuil 20 % sur la pente max lissée à 500 m | Diagnostic `slope_check` (59 boucles réelles, SRTM) : la pente brute (sans lissage) est 1,55× plus élevée en médiane que lissée à 500 m (jusqu'à 2,85×) — un chiffre précis serait trompeur. Aucun seuil testé (10-22 %) n'est stable à plus de ~76 % selon le lissage ; 20 % (67,8 % stable, déclenché sur 11,9 % des boucles) reste un signal qualitatif raisonnable, la différence avec 22 % (76,3 %) n'est pas fiable sur un échantillon de 59 | Dev |
| D18 | 24/09/2026 | Nom de la marque : **Oyan** (remplace le nom de travail « velo-loops », conservé pour le dépôt) | Issu de la plateforme de marque v1 (`docs/brand.md`) ; disponibilité juridique (marque, domaine) encore à vérifier | Branding |
| D19 | 24/09/2026 | Identité visuelle v1 « Argile » : accent unique argile `#A8522F`, neutres basalte/galet/chaux/surface/filet, neutres terrain réservés à la barre de répartition ; Hanken Grotesk + IBM Plex Mono ; logo anneau ouvert + point argile + « yan » ; icône fond chaux. Détail : `docs/brand.md` §13 | Validée par étapes dans Claude Design (moodboard, direction, logo, icône) ; conforme à la direction esthétique (sobre, palette terrain, un seul accent) | Branding / DA |
