// Retour après sortie : tout reste sur l'appareil (localStorage), rien n'est envoyé.
const FB_KEY = 'velo-loops-retours';
let fbSession = []; // secours si le navigateur bloque le stockage

const FB_POS = [
  ['lights', 'Peu de feux'], ['traffic', 'Peu de trafic'], ['city', 'Hors de la ville'],
  ['cycleway', 'Pistes cyclables'], ['flow', 'Tracé fluide'], ['scenery', 'Paysage'],
  ['surface', 'Bon revêtement']
];
const FB_NEG = [
  ['lights', 'Trop de feux'], ['traffic', 'Trop de trafic'], ['city', 'Trop urbain'],
  ['cycleway', 'Manque de pistes'], ['flow', 'Tracé confus, demi-tours'],
  ['scenery', 'Paysage décevant'], ['surface', 'Mauvais revêtement']
];

document.head.insertAdjacentHTML('beforeend', '<style>' +
  '.fb label{display:block;margin:6px 0}' +
  '.fb label.chip{display:inline-block;margin:2px 10px 2px 0}' +
  '.fb select,.fb input[type=number],.fb input[type=text]{font-size:16px;padding:6px;width:100%;box-sizing:border-box}' +
  // boutons secondaires (charte Oyan : l'argile est réservée au bouton GPX)
  '.fb button{font:inherit;font-size:16px;padding:10px 14px;border:1px solid var(--color-text);border-radius:var(--radius-sm);' +
  'background:var(--color-surface);color:var(--color-text);margin:8px 8px 8px 0;cursor:pointer}' +
  '.fb button.sec{border-color:var(--color-border);color:var(--color-text-2)}' +
  '</style>');

function fbLoad() {
  try { return JSON.parse(localStorage.getItem(FB_KEY)) || []; } catch (e) { return []; }
}
function fbStore(list) {
  try { localStorage.setItem(FB_KEY, JSON.stringify(list)); return true; } catch (e) { return false; }
}
const fbCount = () => fbLoad().length + fbSession.length;

const fbChips = (name, items) => items.map(([k, label]) =>
  '<label class="chip"><input type="checkbox" name="' + name + '" value="' + k + '"> ' + label + '</label>').join('');

function feedbackHtml(o) {
  return '<details><summary>J\'ai roulé cette boucle : mon retour</summary>' +
    '<div class="fb">' +
    '<label>Note<select id="fb-rating"><option value="">Choisir</option>' +
    [1, 2, 3, 4, 5].map(n => '<option value="' + n + '">' + '★'.repeat(n) + '</option>').join('') + '</select></label>' +
    '<label>Temps en mouvement (min), comme sur ta montre<input id="fb-min" type="number" inputmode="numeric" min="1"></label>' +
    '<label>Dénivelé positif réel (m), facultatif<input id="fb-asc" type="number" inputmode="numeric" min="0"></label>' +
    '<label>Difficulté ressentie<select id="fb-diff"><option value="">Choisir</option>' +
    '<option value="easy">Trop facile</option><option value="ok">Adaptée</option><option value="hard">Trop dure</option></select></label>' +
    '<p><b>Ce qui était bien</b><br>' + fbChips('fb-pos', FB_POS) + '</p>' +
    '<p><b>Ce qui a gêné</b><br>' + fbChips('fb-neg', FB_NEG) + '</p>' +
    '<label>Commentaire, facultatif<input id="fb-comment" type="text" maxlength="200"></label>' +
    '<button onclick="saveFeedback()">Enregistrer mon retour</button>' +
    '<div id="fb-result"></div>' +
    '<small>Retours sur cet appareil : <span id="fb-count">' + fbCount() + '</span>. ' +
    'Ils ne sont pas envoyés : pense à les exporter.</small><br>' +
    '<button class="sec" onclick="exportFeedback()">Exporter</button>' +
    '<button class="sec" onclick="clearFeedback()">Tout effacer</button>' +
    '</div></details>';
}

function saveFeedback() {
  const o = shown[current];
  const val = id => document.getElementById(id).value;
  const checked = name =>
    [...document.querySelectorAll('input[name="' + name + '"]:checked')].map(e => e.value);
  const out = document.getElementById('fb-result');

  const rating = Number(val('fb-rating'));
  const minutes = Number(val('fb-min'));
  if (!rating || !minutes) {
    out.textContent = 'Indique au moins la note et le temps en mouvement.';
    return;
  }
  const ascent = val('fb-asc') === '' ? null : Number(val('fb-asc'));
  const s = startData.start;
  const entry = indexData && indexData.starts.find(x => x.id === s.id);
  const pt = c => [Number(c[0].toFixed(5)), Number(c[1].toFixed(5))];

  const rec = {
    v: 1,
    saved_at: new Date().toISOString(),
    route: {                       // ce que l'appli avait prévu
      start_key: s.key || (entry && entry.key) || null,
      start_id: s.id,
      start_name: s.name,
      level: o.level,
      duration_target_min: o.duration_target_min == null ? null : o.duration_target_min,
      label: o.label,
      route_key: o.route_key || null,
      distance_km: o.distance_km,
      ascend_m: Math.round(o.ascend_m),
      time_est_min: Math.round(o.time_est_min),
      first: pt(o.coords[0]),
      last: pt(o.coords[o.coords.length - 1])
    },
    real: {                        // ce que l'utilisateur a vécu
      rating: rating,
      minutes: minutes,
      ascent: ascent,
      difficulty: val('fb-diff') || null,
      pos: checked('fb-pos'),
      neg: checked('fb-neg'),
      comment: val('fb-comment').trim()
    }
  };

  const list = fbLoad();
  list.push(rec);
  const stored = fbStore(list);
  if (!stored) fbSession.push(rec);

  const gap = (real, pred) =>
    (real >= pred ? '+' : '−') + Math.abs(Math.round((real - pred) / pred * 100)) + ' %';
  let msg = stored ? 'Merci, retour enregistré. ' : 'Stockage bloqué sur cet appareil : utilise Exporter. ';
  msg += 'Temps : prévu ≈ ' + hm(rec.route.time_est_min) + ', réel ' + hm(minutes) +
    ' (' + gap(minutes, rec.route.time_est_min) + ').';
  if (ascent !== null && rec.route.ascend_m > 0) {
    msg += ' D+ : prévu ' + rec.route.ascend_m + ' m, réel ' + ascent + ' m (' + gap(ascent, rec.route.ascend_m) + ').';
  }
  out.textContent = msg;
  document.getElementById('fb-count').textContent = fbCount();
}

function exportFeedback() {
  const data = { app: 'velo-loops', exported_at: new Date().toISOString(), retours: fbLoad().concat(fbSession) };
  const blob = new Blob([JSON.stringify(data, null, 1)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'oyan-retours.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function clearFeedback() {
  if (!confirm('Effacer tous les retours enregistrés sur cet appareil ?')) return;
  try { localStorage.removeItem(FB_KEY); } catch (e) {}
  fbSession = [];
  document.getElementById('fb-count').textContent = '0';
}
