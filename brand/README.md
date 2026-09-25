# brand/ — fichiers de la marque Oyan

Fichiers sources du logo, de l'icône et des couleurs, exportés de Claude Design (charte v1, validée le 24/09/2026, décision D19).
Règles d'usage et justification : `docs/brand.md` §12-13. **Ne pas modifier ces fichiers à la main** : toute évolution passe par une nouvelle version de la charte.

## Contenu

| Fichier | Usage |
|---|---|
| `oyan-tokens.css` | Variables CSS (couleurs, typographies, rayons, tracé carte). À importer dans l'app. |
| `logo-paths.json` | Tracés vectoriels du logo (anneau + « yan »), pour le redessiner en code si besoin. |
| `svg/oyan-logo-couleur.svg` | Logo principal, sur fond clair (chaux ou blanc). |
| `svg/oyan-logo-sur-basalte.svg` | Logo avec son fond basalte intégré. |
| `svg/oyan-logo-inverse.svg` | Logo clair sans fond, à poser sur fond basalte. |
| `svg/oyan-logo-noir.svg`, `svg/oyan-logo-blanc.svg` | Monochromes (impression une couleur, fond photo). Le noir pur est réservé à ces cas. |
| `svg/oyan-signe-*.svg` | Signe seul (anneau + point), sans « yan ». Taille minimale 12 px. |
| `svg/oyan-icone-app.svg` | Icône d'application (fond chaux, signe à 60 %). |
| `svg/favicon.svg` | Favicon vectoriel (signe à 80 %, trait 7). |
| `png/` | Versions bitmap : logos 512 et 1024 px ; icône 192, 512, 1024 px ; `apple-touch-icon.png` (180 px) ; favicons 16, 32, 48 px. |

## À savoir

- **Pas de photos ici.** Les images du moodboard ont des droits non vérifiés : elles ne doivent jamais entrer dans ce dépôt public.
- **Certificat d'origine (C2PA).** Chaque fichier contient des métadonnées indiquant qu'il a été produit avec Claude. Elles sont gardées dans ces originaux ; les copies utilisées par l'app pourront en être allégées (un favicon passe d'environ 6 Ko à moins de 1 Ko).
- **Publié avec le site.** Ce dossier est copié sur GitHub Pages à la prochaine publication du front, comme le reste du dépôt.
- **Pas de licence libre.** Le dépôt n'a pas de fichier de licence : par défaut, aucun droit de réutilisation du nom et du logo Oyan n'est accordé à des tiers. La protection juridique du nom (dépôt de marque) reste à vérifier par un juriste en propriété intellectuelle.
