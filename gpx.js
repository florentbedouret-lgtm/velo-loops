// textes dans la langue choisie (i18n.js) : nom de la trace, description, attribution

const xmlEsc = s => String(s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[c]));

// points de passage (waypoints) : eau, cafés hors ville, gares et cols nommés, lisibles hors ligne sur le compteur.
// Test de Florent sur Garmin (28/09/2026) : Garmin Connect en fait des « points de parcours » ; il écarte les points loin
// du tracé (gares) et coupe les noms vers 15 caractères. Donc : chaque point est POSÉ SUR LE TRACÉ au km de passage, nom
// court, détail dans <desc>, et <type> / <sym> = types de points de parcours Garmin (Water, Food, Summit, Transport).
const GPX_TYPE = { w: 'Water', c: 'Food', g: 'Transport', col: 'Summit', dirt: 'Danger' };
const GPX_DIRT_MIN_KM = 0.3;     // chemin de terre signalé au compteur à partir de 300 m
const GPX_NAME_MAX = 15;
function gpxWaypoints(o) {
  if (typeof profileData !== 'function' || !o.coords[0] || o.coords[0].length < 2) return '';
  const p = profileData(o.coords, o.distance_km);
  const on = kmv => profilePoint(o.coords, p.dist, Math.max(0, Math.min(o.distance_km, kmv)));
  const clip = str => (str.length > GPX_NAME_MAX ? str.slice(0, GPX_NAME_MAX - 1).trimEnd() + '…' : str);
  const kmTxt = v => v.toFixed(1).replace('.', LANG === 'en' ? '.' : ',');
  const pts = (o.pois || []).map(x => {
    const q = nearestOnRoute(o.coords, [x.lon, x.lat]);   // point du tracé le plus proche (pas le km : plus exact)
    const off = Math.round(crowM(q, [x.lon, x.lat]));
    const short = x.t === 'w' ? t('gpx_w') : withType(t(x.t === 'c' ? 'gpx_c' : 'gpx_g'), x.n);
    const desc = (x.n ? x.n + ' · ' : '') + t('poi_' + x.t) + ' · km ' + kmTxt(x.km) + (off >= 30 ? ' · ' + t('gpx_off', { m: off }) : '');
    return { lon: q[0], lat: q[1], t: x.t, name: clip(short), desc };
  });
  ((o.terrain && o.terrain.climbs) || []).filter(c => c.name).forEach(c => {
    const q = on(c.start_km + c.length_km);
    const named = RELIEF_WORD.test(c.name) ? c.name : t('gpx_col') + ' ' + c.name;   // « Turó d'en Gras » se suffit
    pts.push({ lon: q[0], lat: q[1], t: 'col', name: clip(named), desc: c.name + ' · +' + Math.round(c.gain_m) + ' m' });
  });
  (o.remarkable || []).forEach(x => {                      // lieu remarquable (sommet, belvédère connu)
    const q = on(x.km);
    pts.push({ lon: q[0], lat: q[1], t: 'col', name: clip(x.n), desc: x.n + ' · ' + t('remarkable_gpx') });
  });
  seqRanges(o, 'dirt').filter(([a, b]) => b - a >= GPX_DIRT_MIN_KM).forEach(([a, b]) => {   // début de chaque chemin de terre
    const q = on(a);
    pts.push({ lon: q[0], lat: q[1], t: 'dirt', name: clip(t('gpx_dirt') + ' ' + kmTxt(b - a) + ' km'),
      desc: t('gpx_dirt_desc', { a: kmTxt(a), b: kmTxt(b), l: kmTxt(b - a) }) });
  });
  return pts.map(w => '  <wpt lat="' + w.lat.toFixed(6) + '" lon="' + w.lon.toFixed(6) + '"><name>' + xmlEsc(w.name) +
    '</name><desc>' + xmlEsc(w.desc) + '</desc><sym>' + GPX_TYPE[w.t] + '</sym><type>' + GPX_TYPE[w.t] + '</type></wpt>\n').join('');
}
// nom court « type + lieu » (15 caractères au plus, Garmin) : le lieu est raccourci au premier séparateur
// (« Montcada i Reixac-Manresa » -> « Montcada »), sinon coupé ; le détail complet reste dans <desc> (OsmAnd, Organic Maps…)
const RELIEF_WORD = /^(coll|col|collet|collada|turó|puig|pic|penya|tossal|cim|cima|serra|mola|morro|alto|puerto|mont|montaña|muntanya)(?=[\s'’]|$)/i;
function withType(type, place) {
  if (!place) return type;
  let p = place.split(/\s+-\s+|-|,|\(|\s+(?:i|y|de|del|dels|d')\s+|\s+d'/)[0].trim() || place;
  p = p.charAt(0).toUpperCase() + p.slice(1);
  const room = GPX_NAME_MAX - type.length - 1;
  return type + ' ' + (p.length > room ? p.slice(0, room - 1).trimEnd() + '…' : p);
}
function nearestOnRoute(coords, pt) {                    // projection sur le segment le plus proche (repère local en m)
  const k = Math.cos(pt[1] * Math.PI / 180);
  let best = null, bd = Infinity;
  for (let i = 1; i < coords.length; i++) {
    const ax = (coords[i - 1][0] - pt[0]) * k, ay = coords[i - 1][1] - pt[1];
    const bx = (coords[i][0] - pt[0]) * k, by = coords[i][1] - pt[1];
    const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
    const u = L ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / L)) : 0;
    const qx = ax + u * dx, qy = ay + u * dy, d = qx * qx + qy * qy;
    if (d < bd) { bd = d; best = [pt[0] + qx / k, pt[1] + qy]; }
  }
  return best || [pt[0], pt[1]];
}
// tronçons [km début, km fin] d'une séquence « une lettre tous les 100 m » publiée par le pipeline ; partagé par le
// surlignage de la carte (index.html) et les points du GPX. Suites de points, trous de 100 m ignorés.
const SEQ_KINDS = {
  water: [o => o.scenery && o.scenery.landcover_seq, 'e'], forest: [o => o.scenery && o.scenery.landcover_seq, 'f'],
  town: [o => o.scenery && o.scenery.landcover_seq, 'v'], park: [o => o.scenery && o.scenery.protected_seq, 'p'],
  dirt: [o => o.surface_seq, 'up'], unknown: [o => o.surface_seq, 'n'], main: [o => o.road_seq, 'm'], cycle: [o => o.road_seq, 'c'],
  industrial: [o => o.scenery && o.scenery.industrial_seq, 'i']
};
function seqRanges(o, kind) {
  const k = SEQ_KINDS[kind];
  const seq = (k && k[0](o)) || '', has = c => c !== undefined && k[1].includes(c);
  const step = o.distance_km / Math.max(seq.length, 1), out = [];
  for (let i = 0; i < seq.length;) {
    if (!has(seq[i])) { i++; continue; }
    let j = i;
    while (j < seq.length && (has(seq[j]) || (j + 1 < seq.length && has(seq[j + 1])))) j++;
    out.push([i * step, j * step]);
    i = j;
  }
  return out;
}
// km de chemins de terre (noté ou probable, générateur v9) ; null pour les données plus anciennes (part « non goudronné »)
function dirtKm(o) {
  return o.surface && o.surface.unpaved_probable != null ? o.shares.unpaved * o.distance_km : null;
}
function crowM(a, b) {                                   // distance en mètres entre deux [lon, lat]
  const r = d => d * Math.PI / 180, dLat = r(b[1] - a[1]), dLon = r(b[0] - a[0]);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(r(a[1])) * Math.cos(r(b[1])) * Math.sin(dLon / 2) ** 2;
  return 2 * 6371000 * Math.asin(Math.sqrt(h));
}

function buildGpx(start, o, levelLabel) {
  const title = start.name + ' · ' + (levelLabel || o.level) + ' · ' +
    (typeof optName === 'function' ? optName(o) : t('opt_' + o.label)).toLowerCase() + ' · ' + Math.round(o.distance_km) + ' km';
  const pts = o.coords.map(c =>
    '      <trkpt lat="' + c[1].toFixed(6) + '" lon="' + c[0].toFixed(6) + '">' +
    (c.length > 2 ? '<ele>' + c[2].toFixed(1) + '</ele>' : '') + '</trkpt>').join('\n');
  return '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<gpx version="1.1" creator="Oyan" xmlns="http://www.topografix.com/GPX/1/1">\n' +
    '  <metadata>\n' +
    '    <name>' + xmlEsc(title) + '</name>\n' +
    '    <desc>' + xmlEsc(t('gpx_desc') + ' ' + t('gpx_attr')) + '</desc>\n' +
    '    <copyright author="Contributeurs OpenStreetMap">\n' +
    '      <year>' + new Date().getFullYear() + '</year>\n' +
    '      <license>https://opendatacommons.org/licenses/odbl/</license>\n' +
    '    </copyright>\n' +
    '  </metadata>\n' +
    gpxWaypoints(o) +
    '  <trk>\n' +
    '    <name>' + xmlEsc(title) + '</name>\n' +
    '    <trkseg>\n' + pts + '\n    </trkseg>\n' +
    '  </trk>\n' +
    '</gpx>\n';
}

function downloadGpx(start, o, levelLabel) {
  const blob = new Blob([buildGpx(start, o, levelLabel)], { type: 'application/gpx+xml' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = start.id + '-' + o.level + '-' + o.label + '.gpx';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
