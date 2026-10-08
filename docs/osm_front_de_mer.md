# Front de mer de Poblenou : voies OSM à vérifier sur place

Préparé le 08/10/2026. Les boucles d'Oyan évitent le bord de mer entre le Port Olímpic et le Fòrum parce qu'OSM ne
permet pas au vélo d'y circuler en continu. Cette liste dit quoi regarder sur place, voie par voie.

**Règles :**
- ne corriger OSM que d'après ce que tu vois toi-même sur place : panneaux, marquage au sol, travaux finis ou non ;
- jamais d'après Google (Maps, Street View, vues aériennes) ;
- en cas de doute, une **note OSM** suffit. Sur openstreetmap.org : clic droit sur l'endroit, puis « Ajouter une note ».
  Les contributeurs locaux s'en chargent.

Chaque voie s'ouvre sur `https://www.openstreetmap.org/way/<numéro>`.

## Le long de l'avinguda del Litoral, côté parc (ton passage goudronné)

| voie | ce qu'OSM dit aujourd'hui | à regarder sur place | si c'est le cas |
|---|---|---|---|
| [697164702](https://www.openstreetmap.org/way/697164702) | chemin piéton « Avinguda del Litoral », goudron, **vélo non mentionné** | panneau ou pictogramme vélo ? piste séparée des piétons ? | piste réservée : `highway=cycleway` ; partagée et autorisée : ajouter `bicycle=yes` (ou `designated` si panneau), `segregated=yes/no` |
| [217581788](https://www.openstreetmap.org/way/217581788) | chemin piéton « Avinguda del Litoral », béton, vélo non mentionné | idem | idem |
| [881322862](https://www.openstreetmap.org/way/881322862) | trottoir de l'avenue, goudron | est-ce un trottoir ou une piste cyclable ? | si piste : la décrire comme ci-dessus ; si trottoir : ne rien changer |
| [697167706](https://www.openstreetmap.org/way/697167706) | trottoir de l'avenue, goudron | idem | idem |
| [881322861](https://www.openstreetmap.org/way/881322861), [790881186](https://www.openstreetmap.org/way/790881186) | trottoirs de l'avenue (près de la Llacuna) | idem | idem |

## Promenades au bord de la plage

| voie | ce qu'OSM dit aujourd'hui | à regarder sur place | si c'est le cas |
|---|---|---|---|
| [1533832244](https://www.openstreetmap.org/way/1533832244) | Passeig Marítim del Bogatell **« en travaux »** (donc fermé pour le calcul) | les travaux sont-ils finis ? | travaux finis : demander la mise à jour par une note (le type de voie final dépend de l'aménagement) |
| [949350607](https://www.openstreetmap.org/way/949350607) | Passeig Marítim de la Mar Bella : **« pied à terre »**, compacté | panneau « vélo interdit » ou « pied à terre » ? | sans panneau : le signaler par une note |
| [661025984](https://www.openstreetmap.org/way/661025984) | piste cyclable partagée de 874 m au Bogatell, **compactée**, bon état | revêtement réel : terre stabilisée ou goudron ? | si c'est du goudron : `surface=asphalt` |

## Ce qui ne pose pas de problème (pour mémoire)

- Avinguda del Litoral au Port Olímpic ([280405215](https://www.openstreetmap.org/way/280405215), [280405216](https://www.openstreetmap.org/way/280405216)) : piste cyclable officielle, partagée, pavés.
- Passeig Marítim del Port Olímpic ([4751868](https://www.openstreetmap.org/way/4751868)) : voie piétonne, vélo autorisé.
- Passeig Marítim de la Mar Bella ([496650005](https://www.openstreetmap.org/way/496650005)) : piste cyclable séparée, goudron.

Une fois OSM corrigé, Oyan en tiendra compte au calcul suivant, qui télécharge une carte OSM à jour.
