// Retour après sortie (14/10/2026) : envoyé à Oyan par Web3Forms (formulaire sans serveur, gratuit) ; les retours
// arrivent par e-mail à l'adresse du projet, qui n'apparaît pas dans la page. Rien n'est gardé sur l'appareil.
// La clé d'accès est publique par conception (elle permet seulement d'écrire à cette adresse).
const FB_ENDPOINT = 'https://api.web3forms.com/submit';
const FB_ACCESS_KEY = 'c4c6f664-0a26-4ab6-be29-b001512bf124';

// étiquettes dans la langue choisie (i18n.js : fbp_* ce qui était bien, fbn_* ce qui a gêné) ; les valeurs envoyées
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
  '.fb button:disabled{opacity:.5;cursor:default}' +
  '.fb .fb-r{margin:6px 0}.fb .fb-pills{display:flex;gap:8px;margin-top:6px}' +
  '.fb .fb-pills{gap:2px}.fb button.fb-pill{width:44px;height:44px;margin:0;padding:0;border:0;background:none;' +
  'font-size:30px;line-height:1;color:var(--color-border)}' +
  '.fb button.fb-pill.on{color:var(--color-accent)}' +
  '.fb .fb-trap{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}' +
  '.fb .fb-note{display:block;color:var(--color-text-2);font-size:13px}' +
  '</style>');

const fbChips = (name, items) => items.map(([k, label]) =>
  '<label class="chip"><input type="checkbox" name="' + name + '" value="' + k + '"> ' + label + '</label>').join('');

function feedbackHtml(o) {
  const e = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  return '<details><summary>' + e(t('fb_title')) + '</summary>' +
    '<div class="fb">' +
    // note : cinq étoiles de 44 px (Florent, 02/10/2026 : avec des chiffres, on ne savait pas si 1 ou 5 était le mieux)
    '<div class="fb-r"><span>' + e(t('fb_rating')) + '</span><input type="hidden" id="fb-rating" value="">' +
    '<div class="fb-pills" role="group" aria-label="' + e(t('fb_rating')) + '">' +
    [1, 2, 3, 4, 5].map(n => '<button type="button" class="fb-pill" data-n="' + n + '" aria-pressed="false" aria-label="' +
      e(t('fb_star', { n })) + '" onclick="fbRate(this,' + n + ')">★</button>').join('') + '</div></div>' +
    '<label>' + e(t('fb_min')) + '<input id="fb-min" type="number" inputmode="numeric" min="1"></label>' +
    '<label>' + e(t('fb_asc')) + '<input id="fb-asc" type="number" inputmode="numeric" min="0"></label>' +
    '<label>' + e(t('fb_diff')) + '<select id="fb-diff"><option value="">' + e(t('fb_choose')) + '</option>' +
    '<option value="easy">' + e(t('fb_easy')) + '</option><option value="ok">' + e(t('fb_ok')) + '</option><option value="hard">' +
    e(t('fb_hard')) + '</option></select></label>' +
    '<p><b>' + e(t('fb_pos')) + '</b><br>' + fbChips('fb-pos', FB_POS()) + '</p>' +
    '<p><b>' + e(t('fb_neg')) + '</b><br>' + fbChips('fb-neg', FB_NEG()) + '</p>' +
    '<label>' + e(t('fb_comment')) + '<input id="fb-comment" type="text" maxlength="500" enterkeyhint="done"></label>' +
    // piège à robots : invisible pour les personnes, rempli par les robots (Web3Forms rejette alors l'envoi)
    '<label class="fb-trap" aria-hidden="true">botcheck<input type="checkbox" id="fb-botcheck" tabindex="-1" autocomplete="off"></label>' +
    '<button type="button" id="fb-send" onclick="saveFeedback()">' + e(t('fb_save')) + '</button>' +
    '<div id="fb-result" role="status" aria-live="polite"></div>' +
    '<small class="fb-note">' + e(t('fb_privacy')) + '</small>' +
    '</div></details>';
}

function fbRate(btn, n) {
  document.getElementById('fb-rating').value = String(n);
  btn.parentNode.querySelectorAll('.fb-pill').forEach(b => {
    b.setAttribute('aria-pressed', String(b === btn));
    b.classList.toggle('on', Number(b.dataset.n) <= n);     // étoiles allumées jusqu'à la note choisie
  });
}

async function saveFeedback() {
  const o = shown[current];
  const val = id => document.getElementById(id).value;
  const checked = name =>
    [...document.querySelectorAll('input[name="' + name + '"]:checked')].map(e => e.value);
  const out = document.getElementById('fb-result');
  const btn = document.getElementById('fb-send');

  const rating = Number(val('fb-rating'));
  const minutes = Number(val('fb-min'));
  if (!rating || !minutes) {
    out.textContent = t('fb_need');
    return;
  }
  const ascent = val('fb-asc') === '' ? null : Number(val('fb-asc'));
  const s = startData.start;
  const lvl = t('lvl_' + o.level);
  const dur = o.duration_target_min == null ? '' : hm(o.duration_target_min);

  // un champ par ligne dans l'e-mail reçu (Web3Forms) ; libellés en français, la langue du visiteur est indiquée
  const body = {
    access_key: FB_ACCESS_KEY,
    subject: 'Oyan : retour sur ' + s.name + ', ' + lvl + ', ' + dur,
    from_name: 'Oyan',
    botcheck: document.getElementById('fb-botcheck').checked,
    'Boucle': location.href,
    'Départ': s.name + ' (' + s.id + ')',
    'Allure et durée': lvl + ', ' + dur + ', option « ' + optName(o) + ' »',
    'Prévu': o.distance_km + ' km, ' + Math.round(o.ascend_m) + ' m de D+, ' + hm(o.time_est_min),
    'Note': rating + ' / 5',
    'Temps en mouvement': minutes + ' min',
    'D+ réel': ascent === null ? '' : ascent + ' m',
    'Difficulté': val('fb-diff') || '',
    'Bien': checked('fb-pos').join(', '),
    'Gêné': checked('fb-neg').join(', '),
    'Commentaire': val('fb-comment').trim(),
    'Langue': LANG,
    'Tracé': o.route_key || ''
  };

  btn.disabled = true;
  out.textContent = t('fb_sending');
  let ok = false;
  try {
    const r = await fetch(FB_ENDPOINT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    });
    ok = r.ok && (await r.json()).success === true;
  } catch (err) {
    ok = false;
  }
  btn.disabled = false;
  if (!ok) {
    out.textContent = t('fb_fail');
    return;
  }

  const gap = (real, pred) =>
    (real >= pred ? '+' : '−') + pctStr(Math.abs(Math.round((real - pred) / pred * 100)));
  let msg = t('fb_sent');
  const pred = Math.round(o.time_est_min);
  msg += t('fb_time', { p: hm(pred), r: hm(minutes), g: gap(minutes, pred) });
  if (ascent !== null && o.ascend_m > 0) {
    const pa = Math.round(o.ascend_m);
    msg += t('fb_asc_res', { p: pa, r: ascent, g: gap(ascent, pa) });
  }
  out.textContent = msg;
  btn.disabled = true;                                       // un envoi par boucle affichée
}
