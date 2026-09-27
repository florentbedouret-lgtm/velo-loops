// Textes de l'app en 4 langues. Chaque clé : [français, anglais, espagnol, catalan].
// Français = version de référence (ton Oyan, tutoiement) ; espagnol « tú », catalan « tu ».
// {x} = valeur insérée ; clés « _one / _other » : singulier / pluriel (voir tn).
const LANGS = ['fr', 'en', 'es', 'ca'];
const LANG_NAMES = { ca: 'Català', es: 'Español', fr: 'Français', en: 'English' };
const LANG_KEY = 'oyan-lang';

const I18N = {
  // ---------- réglages
  settings_toggle: ['Afficher ou masquer les réglages', 'Show or hide settings', 'Mostrar u ocultar los ajustes', 'Mostra o amaga els ajustos'],
  lang: ['Langue', 'Language', 'Idioma', 'Idioma'],
  search_btn: ["Chercher l'adresse", 'Search address', 'Buscar la dirección', "Cerca l'adreça"],
  addr_ph: ['Adresse de départ', 'Starting address', 'Dirección de salida', 'Adreça de sortida'],
  locate: ['Utiliser ma position', 'Use my location', 'Usar mi ubicación', 'Fes servir la meva ubicació'],
  pace: ['ALLURE', 'PACE', 'RITMO', 'RITME'],
  duration: ['DURÉE DE LA BOUCLE', 'LOOP DURATION', 'DURACIÓN DE LA RUTA', 'DURADA DE LA RUTA'],
  dur_minus: ['Durée plus courte', 'Shorter', 'Más corta', 'Més curta'],
  dur_plus: ['Durée plus longue', 'Longer', 'Más larga', 'Més llarga'],
  where_hint: ['Tape une adresse, touche la carte ou utilise ta position.', 'Type an address, tap the map or use your location.',
    'Escribe una dirección, toca el mapa o usa tu ubicación.', 'Escriu una adreça, toca el mapa o fes servir la teva ubicació.'],
  change: ['Changer', 'Change', 'Cambiar', 'Canvia'],
  close: ['Fermer', 'Close', 'Cerrar', 'Tanca'],
  filter_ph: ['Filtrer : ville, quartier, gare…', 'Filter: town, district, station…', 'Filtrar: ciudad, barrio, estación…',
    'Filtra: ciutat, barri, estació…'],
  filter_aria: ['Filtrer les départs', 'Filter starting points', 'Filtrar los puntos de salida', 'Filtra els punts de sortida'],
  start_aria: ['Départ', 'Start', 'Salida', 'Sortida'],
  gpx: ['Télécharger le GPX', 'Download GPX', 'Descargar el GPX', 'Descarrega el GPX'],
  choose_start: ['Choisir un départ', 'Choose a start', 'Elige una salida', 'Tria una sortida'],

  // ---------- allures et options
  lvl_facile: ['Tranquille', 'Relaxed', 'Tranquilo', 'Tranquil'],
  lvl_modere: ['Modéré', 'Moderate', 'Moderado', 'Moderat'],
  lvl_soutenu: ['Sportif', 'Sporty', 'Deportivo', 'Esportiu'],
  opt_equilibre: ['Équilibrée', 'Balanced', 'Equilibrada', 'Equilibrada'],
  opt_moins_de_relief: ['Moins de relief', 'Less climbing', 'Menos desnivel', 'Menys desnivell'],
  opt_plus_de_relief: ['Plus de relief', 'More climbing', 'Más desnivel', 'Més desnivell'],
  opt_variante: ['Variante', 'Alternative', 'Variante', 'Variant'],

  // ---------- noms des départs (morceaux en français venus du pipeline)
  station: ['Gare', 'Station', 'Estación', 'Estació'],
  station_note: ['🚆 Gare : vérifie les conditions du transporteur.', "🚆 Station: check the rail operator's rules for bikes.",
    '🚆 Estación: consulta las condiciones del operador.', "🚆 Estació: consulta les condicions de l'operador."],
  dir_nord: ['nord', 'north', 'norte', 'nord'], dir_sud: ['sud', 'south', 'sur', 'sud'],
  dir_est: ['est', 'east', 'este', 'est'], dir_ouest: ['ouest', 'west', 'oeste', 'oest'],
  'dir_nord-est': ['nord-est', 'north-east', 'noreste', 'nord-est'], 'dir_nord-ouest': ['nord-ouest', 'north-west', 'noroeste', 'nord-oest'],
  'dir_sud-est': ['sud-est', 'south-east', 'sureste', 'sud-est'], 'dir_sud-ouest': ['sud-ouest', 'south-west', 'suroeste', 'sud-oest'],

  // ---------- recherche d'adresse et position
  search_min3: ['Tape au moins 3 caractères.', 'Type at least 3 characters.', 'Escribe al menos 3 caracteres.', 'Escriu com a mínim 3 caràcters.'],
  searching: ['Recherche…', 'Searching…', 'Buscando…', 'Cercant…'],
  no_addr: ['Aucune adresse trouvée dans la zone couverte.', 'No address found in the covered area.',
    'No se ha encontrado ninguna dirección en la zona cubierta.', "No s'ha trobat cap adreça a la zona coberta."],
  geo_attr: ['Adresses : Photon (Komoot) · © contributeurs OpenStreetMap', 'Addresses: Photon (Komoot) · © OpenStreetMap contributors',
    'Direcciones: Photon (Komoot) · © colaboradores de OpenStreetMap', "Adreces: Photon (Komoot) · © col·laboradors d'OpenStreetMap"],
  search_down: ["Recherche d'adresse indisponible pour l'instant. Touche la carte ou utilise ta position.",
    'Address search is unavailable right now. Tap the map or use your location.',
    'La búsqueda de direcciones no está disponible ahora mismo. Toca el mapa o usa tu ubicación.',
    "La cerca d'adreces no està disponible ara mateix. Toca el mapa o fes servir la teva ubicació."],
  start_prefix: ['Départ :', 'Start:', 'Salida:', 'Sortida:'],
  approach_na: ["Temps d'approche indisponible pour le moment.", "Travel time to the start isn't available right now.",
    'Tiempo de acceso no disponible por ahora.', "Temps d'accés no disponible de moment."],
  too_far: ['Trop loin : le départ le plus proche est à ≈ {min} min à vélo{range}.', 'Too far: the nearest start is ≈ {min} min away by bike{range}.',
    'Demasiado lejos: la salida más cercana está a ≈ {min} min en bici{range}.', 'Massa lluny: la sortida més propera és a ≈ {min} min en bici{range}.'],
  range: [", jusqu'à ≈ {hi} min", ', up to ≈ {hi} min', ', hasta ≈ {hi} min', ', fins a ≈ {hi} min'],
  see_anyway: ['Voir quand même', 'Show anyway', 'Ver de todos modos', 'Mostra-la igualment'],
  route: ['Itinéraire', 'Directions', 'Cómo llegar', 'Com arribar-hi'],
  bit_far: ['Un peu loin : ', 'A bit far: ', 'Un poco lejos: ', 'Una mica lluny: '],
  approach: ['≈ {min} min à vélo pour le rejoindre{range}', '≈ {min} min by bike to get there{range}',
    '≈ {min} min en bici para llegar{range}', '≈ {min} min en bici per arribar-hi{range}'],
  starts_one: ['1 départ : choisir…', '1 start: choose…', '1 salida: elegir…', '1 sortida: tria…'],
  starts_other: ['{n} départs : choisir…', '{n} starts: choose…', '{n} salidas: elegir…', '{n} sortides: tria…'],
  starts_none: ['Aucun départ ne correspond', 'No matching start', 'Ninguna salida coincide', 'Cap sortida no coincideix'],
  chosen_list: ['Choisi dans la liste', 'Chosen from the list', 'Elegida en la lista', 'Triada a la llista'],
  geo_unavail: ["La localisation n'est pas disponible sur cet appareil.", "Location isn't available on this device.",
    'La ubicación no está disponible en este dispositivo.', 'La ubicació no està disponible en aquest dispositiu.'],
  locating: ['Localisation…', 'Locating…', 'Localizando…', 'Localitzant…'],
  geo_denied: ['Localisation refusée. Tape une adresse ou touche la carte.', 'Location denied. Type an address or tap the map.',
    'Ubicación denegada. Escribe una dirección o toca el mapa.', 'Ubicació denegada. Escriu una adreça o toca el mapa.'],
  neighbour_where: ['Départ voisin, ≈ {min} min à vélo depuis le précédent', 'Nearby start, ≈ {min} min by bike from the previous one',
    'Salida cercana, a ≈ {min} min en bici de la anterior', "Sortida propera, a ≈ {min} min en bici de l'anterior"],
  total_approach: ["+ trajet jusqu'au départ : ≈ {t} au total, sans pauses", '+ ride to the start: ≈ {t} in total, excluding breaks',
    '+ trayecto hasta la salida: ≈ {t} en total, sin pausas', '+ trajecte fins a la sortida: ≈ {t} en total, sense pauses'],

  // ---------- panneau
  loading: ['Chargement…', 'Loading…', 'Cargando…', 'Carregant…'],
  load_error: ['Erreur de chargement ({s})', 'Loading error ({s})', 'Error de carga ({s})', 'Error de càrrega ({s})'],
  no_loop_label: ['Aucune boucle', 'No loop', 'Ninguna ruta', 'Cap ruta'],
  no_loop: ['Pas de boucle de {d} en « {lvl} » depuis ce départ.', 'No {d} loop at “{lvl}” pace from this start.',
    'No hay ruta de {d} a ritmo «{lvl}» desde esta salida.', "No hi ha cap ruta de {d} a ritme «{lvl}» des d'aquesta sortida."],
  other_dur: ['Autre durée possible : ', 'Other possible duration: ', 'Otra duración posible: ', 'Una altra durada possible: '],
  try_level: ['Essaie un autre niveau.', 'Try another pace.', 'Prueba otro ritmo.', 'Prova un altre ritme.'],
  fig_distance: ['DISTANCE', 'DISTANCE', 'DISTANCIA', 'DISTÀNCIA'],
  fig_dplus: ['D+', 'CLIMBING', 'D+', 'D+'],
  fig_duration: ['DURÉE', 'TIME', 'DURACIÓN', 'DURADA'],
  dplus_inline: ['D+ {m} m', '↑ {m} m', 'D+ {m} m', 'D+ {m} m'],
  profile: ['PROFIL', 'PROFILE', 'PERFIL', 'PERFIL'],
  pf_aria: ['Profil altimétrique', 'Elevation profile', 'Perfil de altitud', "Perfil d'altitud"],
  band_aria: ['Milieu traversé', 'Terrain crossed', 'Terreno atravesado', 'Terreny travessat'],
  all_figures: ['Tous les chiffres', 'All the figures', 'Todas las cifras', 'Totes les xifres'],
  notes: ['Durées estimées en mouvement, sans pauses. Dénivelé : estimation.', 'Moving times, excluding breaks. Climbing is an estimate.',
    'Tiempos en movimiento, sin pausas. Desnivel: estimación.', 'Temps en moviment, sense pauses. Desnivell: estimació.'],
  legal_line: ['Parcours indicatifs, à vérifier avant de partir', 'Routes are indicative: check them before you ride',
    'Rutas orientativas: compruébalas antes de salir', 'Rutes orientatives: comprova-les abans de sortir'],
  legal_link: ['Mentions légales & confidentialité', 'Legal notice & privacy', 'Aviso legal y privacidad', 'Avís legal i privadesa'],
  city_pct: ['En ville {p}', 'In town {p}', 'En ciudad {p}', 'A ciutat {p}'],
  warn: ['Attention', 'Warning', 'Atención', 'Atenció'],

  // ---------- barre de terrain
  t_city: ['ville', 'town', 'ciudad', 'ciutat'],
  t_forest: ['forêt', 'forest', 'bosque', 'bosc'],
  t_water: ["bord d'eau", 'waterside', 'junto al agua', "vora l'aigua"],
  t_open: ['espaces ouverts', 'open land', 'espacios abiertos', 'espais oberts'],
  terrain_aria: ['Répartition du terrain : ', 'Terrain breakdown: ', 'Reparto del terreno: ', 'Repartiment del terreny: '],

  // ---------- points clés
  same_as: ["Même parcours qu'en {lvl}.", 'Same route as at {lvl} pace.', 'Mismo recorrido que a ritmo {lvl}.', 'Mateix recorregut que a ritme {lvl}.'],
  same_as_more: ["Même parcours qu'en {lvl}, prolongé de {km} km.", 'Same route as at {lvl} pace, extended by {km} km.',
    'Mismo recorrido que a ritmo {lvl}, alargado {km} km.', 'Mateix recorregut que a ritme {lvl}, allargat {km} km.'],
  same_as_tail: [' Aucune autre boucle différente pour cette durée.', ' No other different loop for this duration.',
    ' No hay otra ruta distinta para esta duración.', ' No hi ha cap altra ruta diferent per a aquesta durada.'],
  k_exit0: ['Tu pars directement hors de la ville.', 'You head straight out of town.', 'Sales directamente de la ciudad.', 'Surts directament de la ciutat.'],
  k_stay: ['Tu restes en ville tout du long.', 'You stay in town the whole way.', 'Te quedas en la ciudad todo el recorrido.',
    'Et quedes a la ciutat tot el recorregut.'],
  k_exit: ['Tu sors de la ville.', 'You leave town.', 'Sales de la ciudad.', 'Surts de la ciutat.'],
  k_protected: ['du parcours traverse un parc ou un espace protégé.', 'of the route runs through a park or protected area.',
    'del recorrido atraviesa un parque o un espacio protegido.', 'del recorregut travessa un parc o un espai protegit.'],
  k_forest_lc: ['du parcours en forêt.', 'of the route in forest.', 'del recorrido por el bosque.', 'del recorregut pel bosc.'],
  k_forest: ['du parcours longe la forêt.', 'of the route runs along forest.', 'del recorrido bordea el bosque.', 'del recorregut voreja el bosc.'],
  k_water: ["du parcours au bord de l'eau.", 'of the route by the water.', 'del recorrido junto al agua.', "del recorregut vora l'aigua."],
  k_main: ['sur des routes principales : reste vigilant.', 'on main roads: stay alert.', 'por carreteras principales: mantente atento.',
    'per carreteres principals: estigues atent.'],
  k_cycle: ['sur pistes cyclables ou voies vertes.', 'on cycle paths or greenways.', 'por carriles bici o vías verdes.', 'per carrils bici o vies verdes.'],
  k_nolight_fig: ['0 feu', '0 lights', '0 semáforos', '0 semàfors'],
  k_nolight: ['Aucun feu tricolore sur le parcours.', 'No traffic lights on the route.', 'Ningún semáforo en el recorrido.', 'Cap semàfor al recorregut.'],
  k_climb: ['Plus longue montée (+{m} m).', 'Longest climb (+{m} m).', 'Subida más larga (+{m} m).', 'Pujada més llarga (+{m} m).'],
  k_steep: ['Au moins une rampe très raide.', 'At least one very steep ramp.', 'Al menos una rampa muy empinada.', 'Com a mínim una rampa molt forta.'],

  // ---------- suggestions
  tip_nature: ['PLUS VITE DANS LA NATURE', 'INTO NATURE FASTER', 'ANTES EN LA NATURALEZA', 'ABANS A LA NATURA'],
  tip_nature_txt: ['Depuis <b>{name}</b>, à ≈ {min} min à vélo, la boucle de {dur} {exit}', 'From <b>{name}</b>, ≈ {min} min away by bike, the {dur} loop {exit}',
    'Desde <b>{name}</b>, a ≈ {min} min en bici, la ruta de {dur} {exit}', 'Des de <b>{name}</b>, a ≈ {min} min en bici, la ruta de {dur} {exit}'],
  exit0: ['part directement hors de la ville.', 'heads straight out of town.', 'sale directamente de la ciudad.', 'surt directament de la ciutat.'],
  exitX: ['sort de la ville après {km} km.', 'leaves town after {km} km.', 'sale de la ciudad tras {km} km.', 'surt de la ciutat després de {km} km.'],
  see_start: ['Voir ce départ', 'See this start', 'Ver esta salida', 'Veure aquesta sortida'],
  longer_label: ['ALLER PLUS LOIN', 'GO FURTHER', 'IR MÁS LEJOS', 'ANAR MÉS LLUNY'],
  longer_txt: ['Une sortie plus longue sort de la ville.', 'A longer ride gets out of town.', 'Una ruta más larga sale de la ciudad.',
    'Una ruta més llarga surt de la ciutat.'],
  longer_btn: ['Essayer plus long', 'Try longer', 'Probar más larga', "Prova'n una de més llarga"],

  // ---------- tous les chiffres
  relief_0: ['plat', 'flat', 'llano', 'pla'], relief_1: ['peu vallonné', 'gently rolling', 'poco ondulado', 'poc ondulat'],
  relief_2: ['vallonné', 'hilly', 'ondulado', 'ondulat'], relief_3: ['très vallonné', 'very hilly', 'muy ondulado', 'molt ondulat'],
  slope_0: ['peu de côtes', 'few climbs', 'pocas subidas', 'poques pujades'], slope_1: ['quelques côtes', 'some climbs', 'algunas subidas', 'algunes pujades'],
  slope_2: ['vallonné', 'hilly', 'ondulado', 'ondulat'], slope_3: ['très vallonné', 'very hilly', 'muy ondulado', 'molt ondulat'],
  f_dist: ['{km} km, D+ {d} m : profil {relief} ({p} m de D+ pour 100 km).', '{km} km, {d} m of climbing: {relief} ({p} m of climbing per 100 km).',
    '{km} km, D+ {d} m: perfil {relief} ({p} m de D+ por cada 100 km).', '{km} km, D+ {d} m: perfil {relief} ({p} m de D+ per cada 100 km).'],
  f_city: ['{p} en ville (zone urbaine dense).', '{p} in town (dense urban area).', '{p} en ciudad (zona urbana densa).', '{p} a ciutat (zona urbana densa).'],
  f_nolights: ['Aucun feu tricolore recensé dans OpenStreetMap.', 'No traffic lights recorded in OpenStreetMap.',
    'Ningún semáforo registrado en OpenStreetMap.', 'Cap semàfor registrat a OpenStreetMap.'],
  f_lights_one: ['1 feu tricolore recensé dans OpenStreetMap ({x} par km).', '1 traffic light recorded in OpenStreetMap ({x} per km).',
    '1 semáforo registrado en OpenStreetMap ({x} por km).', '1 semàfor registrat a OpenStreetMap ({x} per km).'],
  f_lights_other: ['{n} feux tricolores recensés dans OpenStreetMap ({x} par km).', '{n} traffic lights recorded in OpenStreetMap ({x} per km).',
    '{n} semáforos registrados en OpenStreetMap ({x} por km).', '{n} semàfors registrats a OpenStreetMap ({x} per km).'],
  f_climbs_one: ['1 montée de plus de 20 mètres', '1 climb of more than 20 metres', '1 subida de más de 20 metros', '1 pujada de més de 20 metres'],
  f_climbs_other: ['{n} montées de plus de 20 mètres', '{n} climbs of more than 20 metres', '{n} subidas de más de 20 metros', '{n} pujades de més de 20 metres'],
  f_colon: [' :', ':', ':', ':'],
  f_biggest: [', dont les {k} plus grosses :', ', including the {k} biggest:', ', entre ellas las {k} mayores:', ', entre les quals les {k} més grans:'],
  f_climb_item: ['Montée au km {a} : {l} km, +{g} m', 'Climb at km {a}: {l} km, +{g} m', 'Subida en el km {a}: {l} km, +{g} m', 'Pujada al km {a}: {l} km, +{g} m'],
  f_noclimb: ["Aucune montée de plus de 20 mètres d'un seul tenant.", 'No continuous climb of more than 20 metres.',
    'Ninguna subida continua de más de 20 metros.', 'Cap pujada contínua de més de 20 metres.'],
  f_forest: ['{p} dans ou en bordure de forêt.', '{p} in or along forest.', '{p} dentro o al borde del bosque.', '{p} dins o a la vora del bosc.'],
  f_protected: ['{p} dans un parc ou un espace protégé.', '{p} in a park or protected area.', '{p} en un parque o espacio protegido.',
    '{p} en un parc o espai protegit.'],
  f_water: ["{p} à moins de 100 m d'un plan d'eau, d'une rivière ou de la mer.", '{p} within 100 m of a lake, river or the sea.',
    '{p} a menos de 100 m de un lago, un río o el mar.', "{p} a menys de 100 m d'un llac, un riu o el mar."],
  f_views_one: ['1 point de vue à moins de 300 m.', '1 viewpoint within 300 m.', '1 mirador a menos de 300 m.', '1 mirador a menys de 300 m.'],
  f_views_other: ['{n} points de vue à moins de 300 m.', '{n} viewpoints within 300 m.', '{n} miradores a menos de 300 m.', '{n} miradors a menys de 300 m.'],
  f_cycle: ['{p} sur pistes cyclables ou voies vertes.', '{p} on cycle paths or greenways.', '{p} por carriles bici o vías verdes.',
    '{p} per carrils bici o vies verdes.'],
  f_nomain: ['Aucune route principale.', 'No main roads.', 'Ninguna carretera principal.', 'Cap carretera principal.'],
  f_main: ['{p} sur des routes principales.', '{p} on main roads.', '{p} por carreteras principales.', '{p} per carreteres principals.'],
  f_unpaved: ['{p} sur revêtement non goudronné.', '{p} unpaved.', '{p} sin asfaltar.', '{p} sense asfaltar.'],
  f_surface_unknown: ['Revêtement non renseigné dans OpenStreetMap sur {p}.', 'Surface not recorded in OpenStreetMap for {p}.',
    'Firme sin datos en OpenStreetMap en el {p}.', 'Ferm sense dades a OpenStreetMap en el {p}.'],
  f_overlap: ['{p} de tronçons empruntés deux fois.', '{p} of sections ridden twice.', '{p} de tramos recorridos dos veces.',
    '{p} de trams recorreguts dues vegades.'],
  f_uturns_one: ['1 demi-tour sur le parcours.', '1 U-turn on the route.', '1 cambio de sentido en el recorrido.', '1 mitja volta al recorregut.'],
  f_uturns_other: ['{n} demi-tours sur le parcours.', '{n} U-turns on the route.', '{n} cambios de sentido en el recorrido.',
    '{n} mitges voltes al recorregut.'],

  // ---------- GPX
  gpx_desc: ['Boucle générée par Oyan.', 'Loop generated by Oyan.', 'Ruta generada por Oyan.', 'Ruta generada per Oyan.'],
  gpx_attr: ['© contributeurs OpenStreetMap (ODbL) ; calculs GraphHopper (Apache 2.0)', '© OpenStreetMap contributors (ODbL); routing by GraphHopper (Apache 2.0)',
    '© colaboradores de OpenStreetMap (ODbL); cálculo GraphHopper (Apache 2.0)', "© col·laboradors d'OpenStreetMap (ODbL); càlcul GraphHopper (Apache 2.0)"],

  // ---------- retour après sortie (feedback.js)
  fb_title: ["J'ai roulé cette boucle : mon retour", 'I rode this loop: my feedback', 'He hecho esta ruta: mi opinión', 'He fet aquesta ruta: la meva opinió'],
  fb_rating: ['Note', 'Rating', 'Nota', 'Nota'],
  fb_choose: ['Choisir', 'Choose', 'Elegir', 'Tria'],
  fb_min: ['Temps en mouvement (min), comme sur ta montre', 'Moving time (min), as on your watch', 'Tiempo en movimiento (min), como en tu reloj',
    'Temps en moviment (min), com al teu rellotge'],
  fb_asc: ['Dénivelé positif réel (m), facultatif', 'Actual climbing (m), optional', 'Desnivel positivo real (m), opcional', 'Desnivell positiu real (m), opcional'],
  fb_diff: ['Difficulté ressentie', 'Perceived difficulty', 'Dificultad percibida', 'Dificultat percebuda'],
  fb_easy: ['Trop facile', 'Too easy', 'Demasiado fácil', 'Massa fàcil'],
  fb_ok: ['Adaptée', 'Just right', 'Adecuada', 'Adequada'],
  fb_hard: ['Trop dure', 'Too hard', 'Demasiado dura', 'Massa dura'],
  fb_pos: ['Ce qui était bien', 'What was good', 'Lo que estuvo bien', 'El que va estar bé'],
  fb_neg: ['Ce qui a gêné', 'What got in the way', 'Lo que molestó', 'El que va molestar'],
  fb_comment: ['Commentaire, facultatif', 'Comment, optional', 'Comentario, opcional', 'Comentari, opcional'],
  fb_save: ['Enregistrer mon retour', 'Save my feedback', 'Guardar mi opinión', 'Desa la meva opinió'],
  fb_count_pre: ['Retours sur cet appareil : ', 'Feedback on this device: ', 'Opiniones en este dispositivo: ', 'Opinions en aquest dispositiu: '],
  fb_count_post: ['. Ils ne sont pas envoyés : pense à les exporter.', '. They are not sent anywhere: remember to export them.',
    '. No se envían: acuérdate de exportarlas.', ". No s'envien: recorda exportar-les."],
  fb_export: ['Exporter', 'Export', 'Exportar', 'Exporta'],
  fb_clear: ['Tout effacer', 'Delete all', 'Borrar todo', "Esborra-ho tot"],
  fb_need: ['Indique au moins la note et le temps en mouvement.', 'Enter at least the rating and the moving time.',
    'Indica al menos la nota y el tiempo en movimiento.', 'Indica com a mínim la nota i el temps en moviment.'],
  fb_saved: ['Merci, retour enregistré. ', 'Thanks, feedback saved. ', 'Gracias, opinión guardada. ', 'Gràcies, opinió desada. '],
  fb_blocked: ['Stockage bloqué sur cet appareil : utilise Exporter. ', 'Storage is blocked on this device: use Export. ',
    'Almacenamiento bloqueado en este dispositivo: usa Exportar. ', 'Emmagatzematge bloquejat en aquest dispositiu: fes servir Exporta. '],
  fb_time: ['Temps : prévu ≈ {p}, réel {r} ({g}).', 'Time: planned ≈ {p}, actual {r} ({g}).', 'Tiempo: previsto ≈ {p}, real {r} ({g}).',
    'Temps: previst ≈ {p}, real {r} ({g}).'],
  fb_asc_res: [' D+ : prévu {p} m, réel {r} m ({g}).', ' Climbing: planned {p} m, actual {r} m ({g}).', ' D+: previsto {p} m, real {r} m ({g}).',
    ' D+: previst {p} m, real {r} m ({g}).'],
  fb_confirm: ['Effacer tous les retours enregistrés sur cet appareil ?', 'Delete all feedback saved on this device?',
    '¿Borrar todas las opiniones guardadas en este dispositivo?', 'Vols esborrar totes les opinions desades en aquest dispositiu?'],
  fbp_lights: ['Peu de feux', 'Few traffic lights', 'Pocos semáforos', 'Pocs semàfors'],
  fbp_traffic: ['Peu de trafic', 'Little traffic', 'Poco tráfico', 'Poc trànsit'],
  fbp_city: ['Hors de la ville', 'Out of town', 'Fuera de la ciudad', 'Fora de la ciutat'],
  fbp_cycleway: ['Pistes cyclables', 'Cycle paths', 'Carriles bici', 'Carrils bici'],
  fbp_flow: ['Tracé fluide', 'Smooth route', 'Recorrido fluido', 'Recorregut fluid'],
  fbp_scenery: ['Paysage', 'Scenery', 'Paisaje', 'Paisatge'],
  fbp_surface: ['Bon revêtement', 'Good surface', 'Buen firme', 'Bon ferm'],
  fbn_lights: ['Trop de feux', 'Too many traffic lights', 'Demasiados semáforos', 'Massa semàfors'],
  fbn_traffic: ['Trop de trafic', 'Too much traffic', 'Demasiado tráfico', 'Massa trànsit'],
  fbn_city: ['Trop urbain', 'Too urban', 'Demasiado urbano', 'Massa urbà'],
  fbn_cycleway: ['Manque de pistes', 'Not enough cycle paths', 'Faltan carriles bici', 'Falten carrils bici'],
  fbn_flow: ['Tracé confus, demi-tours', 'Confusing route, U-turns', 'Recorrido confuso, cambios de sentido', 'Recorregut confús, mitges voltes'],
  fbn_scenery: ['Paysage décevant', 'Disappointing scenery', 'Paisaje decepcionante', 'Paisatge decebedor'],
  fbn_surface: ['Mauvais revêtement', 'Poor surface', 'Mal firme', 'Mal ferm'],
};

// langue : choix mémorisé sur l'appareil, sinon langue du téléphone, sinon anglais
let LANG = (() => {
  let saved = null;
  try { saved = localStorage.getItem(LANG_KEY); } catch (e) { /* stockage bloqué : langue du téléphone */ }
  if (LANGS.includes(saved)) return saved;
  const nav = (navigator.languages || [navigator.language || '']).map(l => String(l).slice(0, 2).toLowerCase());
  return nav.find(l => LANGS.includes(l)) || 'en';
})();

function t(key, vars) {
  const row = I18N[key];
  let s = row ? (row[LANGS.indexOf(LANG)] != null ? row[LANGS.indexOf(LANG)] : row[0]) : key;
  if (vars) s = s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] != null ? vars[k] : m));
  return s;
}
const tn = (key, n, vars) => t(key + (n === 1 ? '_one' : '_other'), Object.assign({ n }, vars || {}));
const dec = s => (LANG === 'en' ? String(s) : String(s).replace('.', ','));   // séparateur décimal
const pctStr = p => p + (LANG === 'en' ? '%' : ' %');

// textes fixes de la page : data-i18n (texte), data-i18n-ph (placeholder), data-i18n-aria (aria-label), data-i18n-title
function applyStaticI18n(root) {
  const r = root || document;
  r.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  r.querySelectorAll('[data-i18n-ph]').forEach(el => { el.placeholder = t(el.dataset.i18nPh); });
  r.querySelectorAll('[data-i18n-aria]').forEach(el => { el.setAttribute('aria-label', t(el.dataset.i18nAria)); });
  r.querySelectorAll('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
  document.documentElement.lang = LANG;
}
function setLang(l) {
  if (!LANGS.includes(l)) return;
  LANG = l;
  try { localStorage.setItem(LANG_KEY, l); } catch (e) { /* choix gardé pour cette visite seulement */ }
  applyStaticI18n();
  document.dispatchEvent(new CustomEvent('langchange'));
}
