// Retour après sortie : tout reste sur l'appareil (localStorage), rien n'est envoyé.
const FB_KEY = 'velo-loops-retours';
let fbSession = []; // secours si le navigateur bloque le stockage

// étiquettes dans la langue choisie (i18n.js : fbp_* ce qui était bien, fbn_* ce qui a gêné) ; les valeurs enregistrées
// (lights, traffic…) ne changent pas avec la langue
const FB_KEYS = ['lights', 'traffic', 'city', 'cycleway', 'flow', 'scenery', 'surface'];
const FB_POS = () => FB_KEYS.map(k => [k, t('fbp_' + k)]);
const FB_NEG = () => FB_KEYS.map(k => [k, t('fbn_' + k)]);

document.head.insertAdjacentHTML('beforeend', '<style>' +
  '.fb label{display:block;margin:6px 0}' +
  '.fb label.chip{display:inline-block;margin:2px 10px 2px 0}' +
  '.fb select,.fb input[type=number],.fb input[type=text]{font-size:16px;padding:6px;width:100%;box-sizing:border-box}' +
  // boutons secondaires (charte Oyan : l'argile est réservée au bouton GPX)
  '.fb button{font:inherit;font-size:16px;padding:10px 14px;border:1px solid var(--color-text);border-radius:var(--radius-sm);' +
  'background:var(--color-surface);color:var(--color-text);margin:8px 8px 8px 0;cursor:pointer}' +
  '.fb button.sec{border-color:var(--color-border);color:var(--color-text-2)}' +
  '.fb .fb-r{margin:6px 0}.fb .fb-pills{display:flex;gap:8px;margin-top:6px}' +
  '.fb button.fb-pill{width:44px;height:44px;margin:0;padding:0;border-radius:22px;border-color:var(--color-border);' +
  'font-family:var(--font-mono);font-size:15px}' +
  '.fb button.fb-pill[aria-pressed=true]{background:var(--color-text);border-color:var(--color-text);color:#fff}' +
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
  const e = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  return '<details><summary>' + e(t('fb_title')) + '</summary>' +
    '<div class="fb">' +
    // note : cinq pastilles de 44 px sur une ligne (brief v3), valeur gardée dans un champ caché
    '<div class="fb-r"><span>' + e(t('fb_rating')) + '</span><input type="hidden" id="fb-rating" value="">' +
    '<div class="fb-pills" role="group" aria-label="' + e(t('fb_rating')) + '">' +
    [1, 2, 3, 4, 5].map(n => '<button type="button" class="fb-pill" aria-pressed="false" onclick="fbRate(this,' + n + ')">' + n +
      '</button>').join('') + '</div></div>' +
    '<label>' + e(t('fb_min')) + '<input id="fb-min" type="number" inputmode="numeric" min="1"></label>' +
    '<label>' + e(t('fb_asc')) + '<input id="fb-asc" type="number" inputmode="numeric" min="0"></label>' +
    '<label>' + e(t('fb_diff')) + '<select id="fb-diff"><option value="">' + e(t('fb_choose')) + '</option>' +
    '<option value="easy">' + e(t('fb_easy')) + '</option><option value="ok">' + e(t('fb_ok')) + '</option><option value="hard">' +
    e(t('fb_hard')) + '</option></select></label>' +
    '<p><b>' + e(t('fb_pos')) + '</b><br>' + fbChips('fb-pos', FB_POS()) + '</p>' +
    '<p><b>' + e(t('fb_neg')) + '</b><br>' + fbChips('fb-neg', FB_NEG()) + '</p>' +
    '<label>' + e(t('fb_comment')) + '<input id="fb-comment" type="text" maxlength="200"></label>' +
    '<button onclick="saveFeedback()">' + e(t('fb_save')) + '</button>' +
    '<div id="fb-result"></div>' +
    '<small>' + e(t('fb_count_pre')) + '<span id="fb-count">' + fbCount() + '</span>' + e(t('fb_count_post')) + '</small><br>' +
    '<button class="sec" onclick="exportFeedback()">' + e(t('fb_export')) + '</button>' +
    '<button class="sec" onclick="clearFeedback()">' + e(t('fb_clear')) + '</button>' +
    '</div></details>';
}

function fbRate(btn, n) {
  document.getElementById('fb-rating').value = String(n);
  btn.parentNode.querySelectorAll('.fb-pill').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
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
    out.textContent = t('fb_need');
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
    (real >= pred ? '+' : '−') + pctStr(Math.abs(Math.round((real - pred) / pred * 100)));
  let msg = stored ? t('fb_saved') : t('fb_blocked');
  msg += t('fb_time', { p: hm(rec.route.time_est_min), r: hm(minutes), g: gap(minutes, rec.route.time_est_min) });
  if (ascent !== null && rec.route.ascend_m > 0) {
    msg += t('fb_asc_res', { p: rec.route.ascend_m, r: ascent, g: gap(ascent, rec.route.ascend_m) });
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
  if (!confirm(t('fb_confirm'))) return;
  try { localStorage.removeItem(FB_KEY); } catch (e) {}
  fbSession = [];
  document.getElementById('fb-count').textContent = '0';
}
