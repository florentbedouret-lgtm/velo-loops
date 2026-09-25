const EARTH_KM = 6371;
const pfKm = n => n.toFixed(1).replace('.', ',');

// distance cumulée, rééchantillonnage tous les 100 m, lissage sur 500 m
// (même recette que le pipeline pour le D+)
function profileData(coords, distKm) {
  const rad = d => d * Math.PI / 180;
  const dist = [0];
  for (let i = 1; i < coords.length; i++) {
    const [lon1, lat1] = coords[i - 1], [lon2, lat2] = coords[i];
    const dLat = rad(lat2 - lat1), dLon = rad(lon2 - lon1);
    const a = Math.sin(dLat / 2) ** 2 +
      Math.cos(rad(lat1)) * Math.cos(rad(lat2)) * Math.sin(dLon / 2) ** 2;
    dist.push(dist[i - 1] + 2 * EARTH_KM * Math.asin(Math.sqrt(a)));
  }
  // le tracé simplifié est un peu plus court : on recale sur la distance officielle
  const f = distKm / dist[dist.length - 1];
  for (let i = 0; i < dist.length; i++) dist[i] *= f;
  const total = distKm;

  const step = 0.1;
  const n = Math.max(2, Math.round(total / step) + 1);
  const alt = [];
  let j = 0;
  for (let k = 0; k < n; k++) {
    const d = Math.min(k * step, total);
    while (j < dist.length - 2 && dist[j + 1] < d) j++;
    const span = dist[j + 1] - dist[j];
    const t = span > 0 ? (d - dist[j]) / span : 0;
    alt.push(coords[j][2] + t * (coords[j + 1][2] - coords[j][2]));
  }
  const smooth = alt.map((_, k) => {
    let s = 0, c = 0;
    for (let m = Math.max(0, k - 2); m <= Math.min(alt.length - 1, k + 2); m++) { s += alt[m]; c++; }
    return s / c;
  });
  return { total, step, alt: smooth };
}

function profileSvg(o) {
  if (!o.coords[0] || o.coords[0].length < 3) return '';
  const p = profileData(o.coords, o.distance_km);
  const W = 320, H = 120, mL = 34, mR = 8, mT = 8, mB = 20;
  let lo = Math.min(...p.alt), hi = Math.max(...p.alt);
  if (hi - lo < 20) { const mid = (hi + lo) / 2; lo = mid - 10; hi = mid + 10; } // profil presque plat : échelle minimale de 20 m
  lo = Math.floor(lo / 10) * 10;
  hi = Math.ceil(hi / 10) * 10;
  const x = d => mL + (d / p.total) * (W - mL - mR);
  const y = a => mT + (1 - (a - lo) / (hi - lo)) * (H - mT - mB);
  const pts = p.alt.map((a, k) => x(Math.min(k * p.step, p.total)).toFixed(1) + ',' + y(a).toFixed(1));
  const line = 'M' + pts.join(' L');
  const area = line + ' L' + x(p.total).toFixed(1) + ',' + y(lo).toFixed(1) +
    ' L' + x(0).toFixed(1) + ',' + y(lo).toFixed(1) + ' Z';
  // couleurs de brand/oyan-tokens.css : argile (comme le tracé), galet (texte), filet (axe)
  const txt = 'font-size="10" fill="#625D55" font-family="system-ui, sans-serif"';
  return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Profil altimétrique">
    <path d="${area}" fill="#A8522F" fill-opacity="0.15"/>
    <path d="${line}" fill="none" stroke="#A8522F" stroke-width="1.5" stroke-linejoin="round"/>
    <line x1="${mL}" y1="${H - mB}" x2="${W - mR}" y2="${H - mB}" stroke="#E6E2DB"/>
    <text x="${mL - 4}" y="${mT + 8}" text-anchor="end" ${txt}>${hi} m</text>
    <text x="${mL - 4}" y="${H - mB}" text-anchor="end" ${txt}>${lo} m</text>
    <text x="${mL}" y="${H - 6}" ${txt}>0</text>
    <text x="${W - mR}" y="${H - 6}" text-anchor="end" ${txt}>${pfKm(p.total)} km</text>
  </svg>`;
}
