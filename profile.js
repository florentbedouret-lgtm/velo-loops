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
  let lo =
