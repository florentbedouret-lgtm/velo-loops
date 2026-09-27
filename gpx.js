// textes dans la langue choisie (i18n.js) : nom de la trace, description, attribution

const xmlEsc = s => String(s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[c]));

// points de passage (waypoints) : eau, cafés hors ville, gares et cols nommés, lisibles hors ligne sur le compteur
// (Garmin, Wahoo, OsmAnd…) ; <sym> = symboles Garmin courants, ignorés sans dommage par les autres appareils
const GPX_SYM = { w: 'Drinking Water', c: 'Restaurant', g: 'Ground Transportation', col: 'Summit' };
function gpxWaypoints(o) {
  const pts = (o.pois || []).map(x => ({ lat: x.lat, lon: x.lon, t: x.t,
    name: (x.n ? x.n + ' · ' : '') + t('poi_' + x.t) + ' · km ' + x.km.toFixed(1).replace('.', LANG === 'en' ? '.' : ',') }));
  const climbs = ((o.terrain && o.terrain.climbs) || []).filter(c => c.name);
  if (climbs.length && typeof profileData === 'function' && o.coords[0] && o.coords[0].length >= 2) {
    const p = profileData(o.coords, o.distance_km);
    climbs.forEach(c => {
      const q = profilePoint(o.coords, p.dist, Math.min(o.distance_km, c.start_km + c.length_km));
      pts.push({ lat: q[1], lon: q[0], t: 'col', name: c.name + ' · +' + Math.round(c.gain_m) + ' m' });
    });
  }
  return pts.map(w => '  <wpt lat="' + w.lat.toFixed(6) + '" lon="' + w.lon.toFixed(6) + '"><name>' + xmlEsc(w.name) +
    '</name><sym>' + GPX_SYM[w.t] + '</sym><type>' + w.t + '</type></wpt>\n').join('');
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
