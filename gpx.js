const GPX_ATTRIBUTION = '© contributeurs OpenStreetMap (ODbL) ; calculs GraphHopper (Apache 2.0)';
const GPX_LEVELS = { facile: 'tranquille', modere: 'modéré', soutenu: 'sportif' };
const GPX_LABELS = {
  equilibre: 'équilibrée', moins_de_relief: 'moins de relief',
  plus_de_relief: 'plus de relief', variante: 'variante'
};

const xmlEsc = s => String(s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[c]));

function buildGpx(start, o) {
  const title = start.name + ' · ' + (GPX_LEVELS[o.level] || o.level) + ' · ' +
    (GPX_LABELS[o.label] || o.label) + ' · ' + Math.round(o.distance_km) + ' km';
  const pts = o.coords.map(c =>
    '      <trkpt lat="' + c[1].toFixed(6) + '" lon="' + c[0].toFixed(6) + '">' +
    (c.length > 2 ? '<ele>' + c[2].toFixed(1) + '</ele>' : '') + '</trkpt>').join('\n');
  return '<?xml version="1.0" encoding="UTF-8"?>\n' +
    '<gpx version="1.1" creator="velo-loops" xmlns="http://www.topografix.com/GPX/1/1">\n' +
    '  <metadata>\n' +
    '    <name>' + xmlEsc(title) + '</name>\n' +
    '    <desc>' + xmlEsc('Boucle générée par velo-loops. ' + GPX_ATTRIBUTION) + '</desc>\n' +
    '    <copyright author="Contributeurs OpenStreetMap">\n' +
    '      <year>' + new Date().getFullYear() + '</year>\n' +
    '      <license>https://opendatacommons.org/licenses/odbl/</license>\n' +
    '    </copyright>\n' +
    '  </metadata>\n' +
    '  <trk>\n' +
    '    <name>' + xmlEsc(title) + '</name>\n' +
    '    <trkseg>\n' + pts + '\n    </trkseg>\n' +
    '  </trk>\n' +
    '</gpx>\n';
}

function downloadGpx(start, o) {
  const blob = new Blob([buildGpx(start, o)], { type: 'application/gpx+xml' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = start.id + '-' + o.level + '-' + o.label + '.gpx';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
