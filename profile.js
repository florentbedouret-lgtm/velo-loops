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
  return { total, step, alt: smooth, dist };
}

// position [lon, lat] sur le tracé à la distance d (km), par interpolation entre deux points
function profilePoint(coords, dist, d) {
  let j = 0;
  while (j < dist.length - 2 && dist[j + 1] < d) j++;
  const span = dist[j + 1] - dist[j];
  const t = span > 0 ? Math.min(1, Math.max(0, (d - dist[j]) / span)) : 0;
  return [coords[j][0] + t * (coords[j + 1][0] - coords[j][0]), coords[j][1] + t * (coords[j + 1][1] - coords[j][1])];
}

let pfLast = null; // géométrie du dernier profil dessiné, utilisée par bindProfile

// couleurs de terrain de brand/oyan-tokens.css (réservées à la barre de terrain et à cette bande)
const PF_TERRAIN = { v: '#3A3F3B', e: '#8FA3AB', f: '#6E7A5A', o: '#D4C3A3' };

function profileSvg(o) {
  if (!o.coords[0] || o.coords[0].length < 3) return '';
  const p = profileData(o.coords, o.distance_km);
  const seq = (o.scenery && o.scenery.landcover_seq) || '';
  const W = 320, H = seq ? 128 : 120, mL = 34, mR = 8, mT = 8, mB = seq ? 28 : 20;
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
  const txt = 'font-size="10" fill="#625D55" font-family="Hanken Grotesk, system-ui, sans-serif"';
  pfLast = { coords: o.coords, p, W, mL, mR, x, y };
  // bande de terrain : points tous les 100 m recalés sur la distance officielle, segments de même milieu regroupés
  let band = '';
  for (let i = 0; i < seq.length;) {
    let j = i;
    while (j < seq.length && seq[j] === seq[i]) j++;
    const x0 = x(i / seq.length * p.total), x1 = x(j / seq.length * p.total);
    band += `<rect x="${x0.toFixed(1)}" y="${H - mB + 3}" width="${(x1 - x0).toFixed(1)}" height="5" fill="${PF_TERRAIN[seq[i]] || PF_TERRAIN.o}"/>`;
    i = j;
  }
  // touch-action pan-y : un glissement vertical fait défiler la page, un glissement horizontal déplace le curseur
  return `<svg class="pf" viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="Profil altimétrique"
    style="touch-action: pan-y; cursor: crosshair">
    <path d="${area}" fill="#A8522F" fill-opacity="0.15"/>
    <path d="${line}" fill="none" stroke="#A8522F" stroke-width="1.5" stroke-linejoin="round"/>
    <line x1="${mL}" y1="${H - mB}" x2="${W - mR}" y2="${H - mB}" stroke="#E6E2DB"/>
    ${band ? '<g aria-label="Milieu traversé">' + band + '</g>' : ''}
    <text x="${mL - 4}" y="${mT + 8}" text-anchor="end" ${txt}>${hi} m</text>
    <text x="${mL - 4}" y="${H - mB}" text-anchor="end" ${txt}>${lo} m</text>
    <text x="${mL}" y="${H - 6}" ${txt}>0</text>
    <text x="${W - mR}" y="${H - 6}" text-anchor="end" ${txt}>${pfKm(p.total)} km</text>
    <g class="pf-cursor" visibility="hidden">
      <line y1="${mT}" y2="${H - mB}" stroke="#24221F" stroke-width="1"/>
      <circle r="3.5" fill="#24221F" stroke="#FFFFFF" stroke-width="1.5"/>
      <text y="${mT + 8}" font-size="10" fill="#24221F" font-family="Hanken Grotesk, system-ui, sans-serif"></text>
    </g>
  </svg>`;
}

// relie le profil à la carte : onMove([lon, lat]) quand le doigt ou la souris bouge, onMove(null) quand elle sort ;
// readout (facultatif) : élément où écrire « km 6,9 · 30 m » à la place de l'étiquette dessinée dans le profil
function bindProfile(svg, onMove, readout) {
  const g = pfLast;
  const cur = svg.querySelector('.pf-cursor');
  const [line, dot, label] = [cur.querySelector('line'), cur.querySelector('circle'), cur.querySelector('text')];
  const show = e => {
    const r = svg.getBoundingClientRect();
    const vx = (e.clientX - r.left) / r.width * g.W;               // position dans le repère du dessin
    const d = Math.min(1, Math.max(0, (vx - g.mL) / (g.W - g.mL - g.mR))) * g.p.total;
    const a = g.p.alt[Math.min(g.p.alt.length - 1, Math.round(d / g.p.step))];
    const cx = g.x(d);
    line.setAttribute('x1', cx); line.setAttribute('x2', cx);
    dot.setAttribute('cx', cx); dot.setAttribute('cy', g.y(a));
    const txt = 'km ' + pfKm(d) + ' · ' + Math.round(a) + ' m';
    if (readout) readout.textContent = txt; else label.textContent = txt;
    const right = cx > g.W / 2;                                     // étiquette du côté où il reste de la place
    label.setAttribute('x', right ? cx - 5 : cx + 5);
    label.setAttribute('text-anchor', right ? 'end' : 'start');
    cur.setAttribute('visibility', 'visible');
    onMove(profilePoint(g.coords, g.p.dist, d));
  };
  const hide = () => { cur.setAttribute('visibility', 'hidden'); if (readout) readout.textContent = ''; onMove(null); };
  svg.addEventListener('pointerdown', show);
  svg.addEventListener('pointermove', show);
  // au doigt, le point reste affiché après avoir levé le doigt ; à la souris, il disparaît en sortant du profil
  svg.addEventListener('pointerleave', e => { if (e.pointerType === 'mouse') hide(); });
}
