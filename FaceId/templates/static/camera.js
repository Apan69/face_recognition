const $ = id => document.getElementById(id);

// сессия истекла или входа не было - вернуться на страницу входа
const _fetch = window.fetch.bind(window);
window.fetch = async (...args) => { const r = await _fetch(...args); if (r.status === 401) location.href = '/login'; return r; };

// уровень доступа (роль оператора Perco): 1 - «Камера и проходы», 2 - + «Сотрудники и фото», 3 - + «Конфиг».
// Права проверяет и сервер; здесь лишние вкладки только скрываются.
let level = 1;
const ALL_TABS = ['cam', 'staff', 'config'];
let TABS = ALL_TABS.slice(0, 1);
function applyLevel(l) {
  level = Math.max(1, Math.min(3, Number(l) || 1));
  TABS = ALL_TABS.slice(0, level);
  ALL_TABS.forEach(t => { $('tabBtn-' + t).style.display = TABS.includes(t) ? '' : 'none'; });
  document.querySelector('.tabs').style.display = TABS.length < 2 ? 'none' : '';   // одна вкладка - панель вкладок не нужна
}
async function loadMe() {
  try {
    const j = await (await fetch('/api/me')).json();
    applyLevel(j.level);
    if (j.auth) { $('meLogin').textContent = 'Вы вошли как ' + j.login; $('userBar').classList.remove('hidden'); }
  } catch (e) { applyLevel(1); }
}
async function logout() {
  try { await fetch('/logout', { method: 'POST' }); } catch (e) {}
  location.href = '/login';
}
let status = null, users = [];
let videoOn = true;
let currentTab = 'cam';
const esc = v => String(v).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
try { videoOn = localStorage.getItem('videoOn') !== '0'; } catch (e) {}

// ------------------------------------------------------------------ статус
async function refreshStatus() {
  try {
    const r = await fetch('/api/status');
    status = await r.json();
  } catch (e) { $('status').textContent = 'нет связи с сервером'; return; }
  const s = status;
  const parts = [
    `сервис: <b class="${s.state === 'running' ? 'ok' : 'bad'}">${esc(s.state)}</b>`,
    `камера: <b class="${s.camera_connected ? 'ok' : 'bad'}">${s.camera_connected ? 'ок' : 'нет сигнала'}</b>`,
    `кадров/с: <b>${s.loop_fps}</b>`,
    `режим: <b>${esc(s.mode || '-')}</b>${s.test_mode ? ' (тест, турникет не открывается)' : ''}`,
    `сотрудников: <b>${s.users_total}</b>, проходят по камере: <b>${s.users}</b>, эмбеддингов: <b>${s.embeddings}</b>`,
  ];
  if (s.stream_encoder) parts.push(`кодек: <b>${esc(s.stream_encoder)}</b>`);
  if (s.error) parts.push(`<b class="bad">ошибка: ${esc(s.error)}</b>`);
  $('status').innerHTML = parts.map(p => `<span>${p}</span>`).join('');   // каждый пункт - один блок, чтобы подпись и значение не разъезжались
  divError = s.divisions_error || '';
  if (s.divisions_version !== lastDivVersion) { lastDivVersion = s.divisions_version; if (level >= 2) { loadDivisions(); loadUsers(); } }   // подразделения загрузились/обновились
  const srcChanged = (s.passes_source || 'local') !== passesSource || (s.passes_error || '') !== passesError ||
                     !!s.passes_loading !== passesLoading;
  renderLastPass(s.last_pass);
  const mChanged = s.manual_in !== manualIn || s.manual_out !== manualOut;
  manualIn = s.manual_in || 0; manualOut = s.manual_out || 0;
  passesSource = s.passes_source || 'local';
  passesError = s.passes_error || ''; passesLoading = !!s.passes_loading;
  if (s.events_version !== lastEventsVersion || srcChanged || mChanged) { lastEventsVersion = s.events_version; loadEvents(); }   // появились новые проходы
  if (videoOn && currentTab === 'cam' && s.play_path && !playerPc && !playerTimer) startPlayer();
}

// ------------------------------------------------------------------ WebRTC-просмотр (WHEP через сервер)
let playerPc = null, playerTimer = null;

function waitIce(pc) {
  return new Promise(resolve => {
    if (pc.iceGatheringState === 'complete') return resolve();
    const done = () => { if (pc.iceGatheringState === 'complete') { pc.removeEventListener('icegatheringstatechange', done); resolve(); } };
    pc.addEventListener('icegatheringstatechange', done);
    setTimeout(resolve, 3000);
  });
}

function retryPlayer() {
  if (playerTimer || !videoOn || currentTab !== 'cam') return;
  playerTimer = setTimeout(() => { playerTimer = null; startPlayer(); }, 2000);
}

function stopPlayer() {
  if (playerTimer) { clearTimeout(playerTimer); playerTimer = null; }
  if (playerPc) { playerPc.onconnectionstatechange = null; playerPc.close(); playerPc = null; }
  $('player').srcObject = null;
}

function setVideo(on) {
  videoOn = on;
  try { localStorage.setItem('videoOn', on ? '1' : '0'); } catch (e) {}
  $('videoSwitch').checked = on;
  $('player').classList.toggle('hidden', !on);
  $('videoOff').classList.toggle('hidden', on);
  $('videoCard').classList.toggle('compact', !on);
  document.body.classList.toggle('novideo', !on);   // таблица проходов занимает освободившееся место
  if (on) startPlayer(); else stopPlayer();
}
$('videoSwitch').addEventListener('change', e => setVideo(e.target.checked));

async function startPlayer() {
  if (!videoOn || currentTab !== 'cam') return;   // видео идёт только на вкладке «Камера»
  if (playerPc) { playerPc.onconnectionstatechange = null; playerPc.close(); playerPc = null; }
  if (!status || !status.play_path) return;
  const pc = new RTCPeerConnection();
  playerPc = pc;
  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.ontrack = e => { $('player').srcObject = e.streams[0]; };
  pc.onconnectionstatechange = () => {
    if (pc === playerPc && ['failed', 'disconnected', 'closed'].includes(pc.connectionState)) {
      playerPc = null; retryPlayer();
    }
  };
  try {
    await pc.setLocalDescription(await pc.createOffer());
    await waitIce(pc);
    const r = await fetch('/webrtc/whep/' + status.play_path, {
      method: 'POST', headers: { 'Content-Type': 'application/sdp' }, body: pc.localDescription.sdp });
    if (!r.ok) throw new Error('WHEP ' + r.status);
    await pc.setRemoteDescription({ type: 'answer', sdp: await r.text() });
  } catch (e) {
    console.warn('player:', e);
    if (pc === playerPc) { pc.onconnectionstatechange = null; pc.close(); playerPc = null; retryPlayer(); }
  }
}

// ------------------------------------------------------------------ вкладки
function initialTab() {
  const fromHash = (location.hash || '').slice(1);
  if (TABS.includes(fromHash)) return fromHash;
  try { const t = localStorage.getItem('tab'); if (TABS.includes(t)) return t; } catch (e) {}
  return 'cam';
}
function showTab(name, save = true) {
  if (!TABS.includes(name)) name = 'cam';
  currentTab = name;
  TABS.forEach(t => {
    $('tab-' + t).classList.toggle('hidden', t !== name);
    $('tabBtn-' + t).classList.toggle('active', t === name);
  });
  if (name !== 'staff') camStop();                                   // веб-камера не остаётся включённой на другой вкладке
  if (videoOn && name === 'cam') startPlayer(); else stopPlayer();   // видео не тянется, пока открыта другая вкладка
  if (name === 'staff' && save) loadUsers();                         // свежие статусы сотрудников
  if (name === 'config' && !$('sub-log').classList.contains('hidden')) loadLog();   // журнал - подвкладка по умолчанию
  if (save) {
    try { localStorage.setItem('tab', name); } catch (e) {}
    if (window.history && history.replaceState) history.replaceState(null, '', '#' + name);
  }
}

// ------------------------------------------------------------------ кто входил и выходил
// последний вошедший / вышедший - крупно рядом с заголовком
function renderLastPass(p) {
  const el = $('lastPass');
  if (!p) { el.classList.add('hidden'); return; }
  const dir = p.direction === 'entrance' ? ['➡ вход', 'ok'] : p.direction === 'exit' ? ['⬅ выход', 'warn'] : ['проход', 'hint'];
  el.className = dir[1];
  el.textContent = `${dir[0]}: ${p.user} · ${fmtTime(p.ts)}`;
}
let manualIn = 0, manualOut = 0;
let lastEventsVersion = null, passesSource = 'local', passesError = '', passesLoading = false;
const insideOnly = () => $('insideOnly').checked;
let eventRows = [];
const norm = t => String(t || '').toLowerCase().replace(/ё/g, 'е');
const filterActive = () => !!($('fltName').value.trim() || $('fltDiv').value);
async function loadEvents() {
  // без фильтра - последние 300 проходов; с фильтром - больше, чтобы найти человека за весь день
  const url = insideOnly() ? '/api/events?inside=1' : '/api/events?limit=' + (filterActive() ? 3000 : 300);
  let rows;
  try { rows = await (await fetch(url)).json(); } catch (e) { return; }
  eventRows = Array.isArray(rows) ? rows : [];
  renderEvents();
}
$('insideOnly').checked = (() => { try { return localStorage.getItem('insideOnly') === '1'; } catch (e) { return false; } })();
$('insideOnly').addEventListener('change', () => {
  try { localStorage.setItem('insideOnly', insideOnly() ? '1' : '0'); } catch (e) {}
  loadEvents();
});
// фильтры: по фамилии (ФИО) и подразделению; список подразделений берётся из самих проходов
let fltWasActive = false;
function onFilterChange() {
  const active = filterActive();
  if (active !== fltWasActive) { fltWasActive = active; loadEvents(); return; }   // включили/выключили - перечитать с другим лимитом
  renderEvents();
}
$('fltName').addEventListener('input', onFilterChange);
$('fltDiv').addEventListener('change', onFilterChange);
function refreshDivFilter() {
  const sel = $('fltDiv'), keep = sel.value;
  const names = [...new Set(eventRows.map(e => e.division).filter(Boolean))].sort((x, y) => String(x).localeCompare(String(y), 'ru'));
  if (keep && !names.includes(keep)) names.push(keep);          // выбранное подразделение не пропадает, даже если проходов нет
  sel.innerHTML = '';
  const add = (v, t) => { const o = document.createElement('option'); o.value = v; o.textContent = t; sel.appendChild(o); };
  add('', 'Все подразделения');
  names.forEach(n => add(n, n));
  sel.value = keep;
}
function fmtTime(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const time = d.toLocaleTimeString('ru-RU');
  return d.toDateString() === now.toDateString() ? time : d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit' }) + ' ' + time;
}
function renderEvents() {
  const body = $('eventsBody'), inside = insideOnly();
  refreshDivFilter();
  const q = norm($('fltName').value.trim()), dv = $('fltDiv').value;
  const rows = eventRows.filter(e => (!q || norm(e.user).includes(q)) && (!dv || e.division === dv));
  body.innerHTML = '';
  $('eventsEmpty').textContent = eventRows.length && !rows.length ? 'По фильтру ничего не найдено'
    : inside ? 'В здании сейчас никого нет' : 'Проходов пока не было';
  $('eventsEmpty').classList.toggle('hidden', rows.length > 0);
  $('eventsWrap').classList.toggle('hidden', rows.length === 0);
  const manual = eventRows.filter(e => e.method === 'button' || e.method === 'operator').length;   // без идентификатора
  const shown = filterActive() ? `${rows.length} из ${eventRows.length}` : `${rows.length}`;
  $('eventsInfo').textContent = (inside ? `в здании по идентификатору: ${shown}` + (manualIn || manualOut ? ` · по кнопке: вошло ${manualIn}, вышло ${manualOut} · всего примерно: ${Math.max(0, eventRows.length + manualIn - manualOut)}` : '')
      : `проходов: ${shown}` + (manual ? ` (из них без идентификатора: ${manual})` : '')) +
    (passesSource === 'perco'
      ? ' · журнал Perco (карты и лицо)' + (passesLoading ? ' · идёт загрузка за сегодня...' : '')
      : passesLoading ? ' · журнал Perco загружается...'
      : ' · только проходы по лицу с этой камеры (журнал Perco недоступен' + (passesError ? ': ' + passesError : '') + ')');
  rows.forEach(ev => {
    const tr = document.createElement('tr');
    const dir = ev.direction === 'entrance' ? ['➡ вход', 'ok'] : ev.direction === 'exit' ? ['⬅ выход', 'warn'] : ['проход', 'hint'];
    const how = ({ card: 'карта', face: 'лицо', button: 'кнопка вахтёра', operator: 'команда оператора' }[ev.method] || 'лицо') + (ev.test ? ' (тест)' : '');
    const bad = ev.opened === false && !ev.test;
    [[fmtTime(ev.ts), ''], [ev.user, ''], [ev.division || '', 'hint'], dir,
     [bad ? how + ' ⚠ турникет не открылся' : how, bad ? 'bad' : 'hint']].forEach(([text, cls]) => {
      const td = document.createElement('td');
      td.textContent = text;            // имена - только как текст, не как HTML
      if (cls) td.className = cls;
      tr.appendChild(td);
    });
    body.appendChild(tr);
  });
}

// ------------------------------------------------------------------ список сотрудников
let divisions = [], divTree = [], lastDivVersion = null, divError = '';
const savedDivision = () => { try { return localStorage.getItem('division') || ''; } catch (e) { return ''; } };

async function loadUsers() {
  try { users = await (await fetch('/api/users')).json(); } catch (e) { users = []; }
  renderDivisions(); renderUsers();
}
async function loadDivisions() {
  try { divisions = await (await fetch('/api/divisions')).json(); } catch (e) { divisions = []; }
  renderDivisions(); renderUsers();
}
// плоский список подразделений (без иерархии): каждый сотрудник относится ровно к одному подразделению
function renderDivisions() {
  const sel = $('divSel');
  const keep = sel.value || savedDivision();
  divTree = divisions.slice().sort((x, y) => String(x.name).localeCompare(String(y.name), 'ru'));
  sel.innerHTML = '';
  if (divTree.length === 0 && divError) {   // Perco не отдал подразделения: показываем причину, а не прячем список
    const o = document.createElement('option');
    o.textContent = 'Подразделения недоступны: ' + divError;
    sel.appendChild(o);
    sel.disabled = true; sel.title = divError; sel.classList.remove('hidden');
    return;
  }
  sel.disabled = false; sel.title = 'Подразделение';
  sel.classList.toggle('hidden', divTree.length === 0);   // источник Excel - список скрыт
  const add = (value, text) => { const o = document.createElement('option'); o.value = value; o.textContent = text; sel.appendChild(o); };
  add('', 'Все подразделения');
  divTree.forEach(d => {
    const cnt = users.filter(u => u.division === d.id).length;
    add(String(d.id), `${d.name || '(без названия)'} (${cnt})`);
  });
  sel.value = divTree.some(d => String(d.id) === keep) ? keep : '';
}
function divisionIds() {   // id выбранного подразделения; null - все подразделения
  const v = $('divSel').value;
  return v ? new Set([Number(v)]) : null;
}
function renderUsers() {
  const q = $('userFilter').value.trim().toLowerCase();
  const keep = $('userSel').value;
  const sel = $('userSel'); sel.innerHTML = '';
  const onlyOff = $('onlyOff').checked;
  const ids = divisionIds();
  const rows = users.filter(u => (!onlyOff || !u.active) &&
                                 (!ids || (u.division != null && ids.has(u.division))) &&
                                 (!q || String(u.id).includes(q) || String(u.name).toLowerCase().includes(q)));
  rows.forEach(u => {
      const o = document.createElement('option');
      o.value = u.id;
      const photo = u.embedding ? '  ✓' : u.photo ? '  фото без эмбеддинга' : '  — нет фото';
      const noEmb = u.active && !u.embedding;          // проход по лицу включён, а файла эмбеддинга нет
      o.textContent = `${u.name}  (${u.id})${u.active ? '' : '  ⛔'}${noEmb ? '  ⚠' : ''}${photo}`;
      if (!u.active) o.className = 'off';
      else if (noEmb) o.className = 'noemb';
      if (String(u.id) === keep) o.selected = true;
      sel.appendChild(o);
    });
  const off = users.filter(u => !u.active).length;
  const noEmb = users.filter(u => u.active && !u.embedding).length;
  $('userHint').textContent = `✓ — есть эмбеддинг (фото обработано). ⛔ (оранжевым) — проход по лицу выключен: по камере такой сотрудник ` +
    `не пройдёт, но фото можно подготовить заранее. ⚠ (красным) — проход по лицу включён, но файла эмбеддинга нет: камера его не узнает. ` +
    `Показано: ${rows.length} из ${users.length}, с выключенным проходом: ${off}, без эмбеддинга при включённом проходе: ${noEmb}`;
  updateSend();
}
$('userFilter').addEventListener('input', renderUsers);
$('onlyOff').addEventListener('change', renderUsers);
$('divSel').addEventListener('change', () => { try { localStorage.setItem('division', $('divSel').value); } catch (e) {} renderUsers(); });
$('userSel').addEventListener('change', updateSend);

// ------------------------------------------------------------------ фото: веб-камера этого компьютера
let camStream = null, shotBlob = null;

const savedCam = () => { try { return localStorage.getItem('camId') || ''; } catch (e) { return ''; } };

async function openCam(deviceId) {
  const base = { width: { ideal: 1280 }, height: { ideal: 720 } };
  const video = deviceId ? { ...base, deviceId: { exact: deviceId } } : base;
  return navigator.mediaDevices.getUserMedia({ video, audio: false });
}

// список веб-камер; показывается, только если их больше одной
async function refreshCams() {
  const sel = $('camSel');
  let cams = [];
  try { cams = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput' && d.deviceId); } catch (e) {}
  const active = camStream && camStream.getVideoTracks()[0] ? camStream.getVideoTracks()[0].getSettings().deviceId : '';
  const keep = active || sel.value || savedCam();
  sel.innerHTML = '';
  cams.forEach((d, i) => {
    const o = document.createElement('option');
    o.value = d.deviceId;
    o.textContent = d.label || ('Камера ' + (i + 1));
    sel.appendChild(o);
  });
  if (cams.some(d => d.deviceId === keep)) sel.value = keep;
  sel.classList.toggle('hidden', cams.length < 2);
}

async function camToggle() {
  if (camStream) return camStop();
  await camStart();
}

async function camStart() {
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    $('camHint').textContent = 'Камера в браузере работает только по HTTPS или на localhost. ' +
      'Откройте страницу по https:// (сертификат самоподписанный - подтвердите исключение) или загрузите файл.';
    return;
  }
  const wanted = $('camSel').value || savedCam();
  let stream;
  try {
    try { stream = await openCam(wanted); }
    catch (e) {
      if (wanted && (e.name === 'OverconstrainedError' || e.name === 'NotFoundError')) stream = await openCam('');   // сохранённой камеры больше нет
      else throw e;
    }
  } catch (e) { $('camHint').textContent = 'Нет доступа к камере: ' + e.message; return; }
  camStream = stream;
  $('camVideo').srcObject = camStream;
  $('camVideo').classList.remove('hidden');
  $('shot').classList.add('hidden');
  $('camBtn').textContent = 'Выключить камеру';
  $('shootBtn').disabled = false;
  $('camHint').textContent = '';
  const id = camStream.getVideoTracks()[0].getSettings().deviceId;
  if (id) { try { localStorage.setItem('camId', id); } catch (e) {} }
  refreshCams();   // названия камер доступны только после разрешения доступа
}

// смена камеры во время съёмки: перезапуск потока с новой камерой
$('camSel').addEventListener('change', async () => {
  try { localStorage.setItem('camId', $('camSel').value); } catch (e) {}
  if (!camStream) return;
  const hadShot = !$('shot').classList.contains('hidden') && shotBlob;
  camStop();
  await camStart();
  if (hadShot) { resetShot(); }
});
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', refreshCams);   // камеру подключили / отключили
}
function camStop() {
  if (camStream) camStream.getTracks().forEach(t => t.stop());
  camStream = null;
  $('camVideo').srcObject = null;
  $('camBtn').textContent = 'Включить камеру';
  $('shootBtn').textContent = 'Снять';
  $('shootBtn').disabled = true;
}
// убрать снимок и вернуть живое изображение веб-камеры (если она включена)
function resetShot() {
  shotBlob = null;
  $('shot').classList.add('hidden');
  $('fileInput').value = '';
  $('shootBtn').textContent = 'Снять';
  if (camStream) $('camVideo').classList.remove('hidden');
  updateSend();
}
function shoot() {
  const v = $('camVideo'), c = $('shot');
  if (!c.classList.contains('hidden') && camStream) { resetShot(); return; }   // «Переснять»
  if (!v.videoWidth) return;
  c.width = v.videoWidth; c.height = v.videoHeight;
  c.getContext('2d').drawImage(v, 0, 0);
  c.toBlob(b => { shotBlob = b; c.classList.remove('hidden'); v.classList.add('hidden'); $('shootBtn').textContent = 'Переснять'; setMsg(''); updateSend(); }, 'image/jpeg', 0.92);
}
// двойной клик по изображению веб-камеры = кнопка «Снять»
$('camVideo').addEventListener('dblclick', e => { e.preventDefault(); if (camStream) shoot(); });
$('camVideo').title = 'Двойной клик - снять';
$('fileInput').addEventListener('change', e => {
  const f = e.target.files[0];
  if (!f) return;
  shotBlob = f;
  const img = new Image();
  img.onload = () => {
    const c = $('shot'); c.width = img.naturalWidth; c.height = img.naturalHeight;
    c.getContext('2d').drawImage(img, 0, 0);
    c.classList.remove('hidden'); $('camVideo').classList.add('hidden');
    URL.revokeObjectURL(img.src);
  };
  img.src = URL.createObjectURL(f);
  setMsg(''); updateSend();
});

// переключатель «не спрашивать подтверждение» запоминается в браузере
try { $('noConfirm').checked = localStorage.getItem('noConfirm') === '1'; } catch (e) {}
// «не спрашивать подтверждение» заодно убирает подсказку-расшифровку под списком сотрудников (для опытных пользователей)
const applyNoConfirm = () => $('userHint').classList.toggle('hidden', $('noConfirm').checked);
applyNoConfirm();
$('noConfirm').addEventListener('change', e => {
  try { localStorage.setItem('noConfirm', e.target.checked ? '1' : '0'); } catch (err) {}
  applyNoConfirm();
});
const sure = text => $('noConfirm').checked || confirm(text);

function setMsg(text, cls) { const m = $('photoMsg'); m.textContent = text; m.className = cls || 'hint'; }
function selectedUser() {
  const id = $('userSel').value;
  return id ? users.find(x => String(x.id) === id) : null;
}
function updateSend() {
  const u = selectedUser();
  const cu = $('curUser');
  cu.textContent = u ? `${u.name} (${u.id})` : 'сотрудник не выбран';
  cu.classList.toggle('none', !u);
  $('sendBtn').disabled = !(shotBlob && u);
  $('enableBtn').disabled = !(u && (shotBlob || u.embedding));        // нужно новое фото или уже готовый эмбеддинг
  $('disableBtn').disabled = !(u && (u.embedding || u.photo || u.active));
}

async function sendPhoto() {
  const id = $('userSel').value;
  if (!id || !shotBlob) return;
  const fd = new FormData();
  fd.append('user_id', id);
  fd.append('photo', shotBlob, 'photo.jpg');
  $('sendBtn').disabled = true; setMsg('отправка…');
  try {
    const r = await fetch('/api/photo', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) setMsg('❌ ' + j.detail, 'err');
    else { setMsg('✅ Фото сохранено: ' + j.name + (j.active ? '' : ' (проход по лицу выключен - по камере пока не пройдёт)'), 'good'); resetShot(); loadUsers(); refreshStatus(); }
  } catch (e) { setMsg('⚠️ ' + e, 'err'); }
  updateSend();
}

const PERCO_TEXT = {
  set: 'отметка в Perco записана', already: 'отметка в Perco уже была', skipped: 'отметка в Perco не требуется (источник - Excel)',
  cleared: 'отметка в Perco снята', none: 'отметки в Perco не было',
};

async function enablePass() {
  const u = selectedUser();
  if (!u) return;
  if (!sure(`Включить проход по лицу для «${u.name}» (ID ${u.id})?\nВ Perco будет записана отметка прохода по лицу (значение = ID).` +
               (shotBlob ? '' : '\nНового фото нет - будет использовано уже сохранённое.'))) return;
  const fd = new FormData();
  fd.append('user_id', u.id);
  if (shotBlob) fd.append('photo', shotBlob, 'photo.jpg');
  $('enableBtn').disabled = true; $('disableBtn').disabled = true; $('sendBtn').disabled = true; setMsg('включаю…');
  try {
    const r = await fetch('/api/enable-pass', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) setMsg('❌ ' + j.detail, 'err');
    else {
      const parts = [];
      if (j.enrolled) parts.push('фото обработано, эмбеддинг сохранён');
      parts.push(j.perco === 'error' ? '⚠ отметка в Perco НЕ записана: ' + j.perco_error : PERCO_TEXT[j.perco]);
      const ok = j.active && j.perco !== 'error';
      setMsg((ok ? '✅ Проход по лицу включён: ' : '⚠️ ') + j.name + ' - ' + parts.join('; ') +
             (!j.active && j.perco === 'set' ? '. Perco пока не отдаёт сотрудника среди включённых - нажмите «Синхронизировать»' : ''),
             ok ? 'good' : 'err');
      resetShot();
    }
    loadUsers(); refreshStatus();
  } catch (e) { setMsg('⚠️ ' + e, 'err'); }
  updateSend();
}

async function disablePass() {
  const u = selectedUser();
  if (!u) return;
  if (!sure(`Убрать проход по лицу для «${u.name}» (ID ${u.id})?\n` +
               'Фото и эмбеддинг будут убраны в архив, отметка в Perco будет снята. Чтобы вернуть проход, понадобится новое фото.')) return;
  const fd = new FormData();
  fd.append('user_id', u.id);
  $('enableBtn').disabled = true; $('disableBtn').disabled = true; $('sendBtn').disabled = true; setMsg('убираю…');
  try {
    const r = await fetch('/api/disable-pass', { method: 'POST', body: fd });
    const j = await r.json();
    if (!r.ok) setMsg('❌ ' + j.detail, 'err');
    else {
      const parts = [];
      if (j.removed) parts.push('фото и эмбеддинг убраны в архив');
      parts.push(j.perco === 'error' ? '⚠ отметка в Perco НЕ снята: ' + j.perco_error : PERCO_TEXT[j.perco]);
      const ok = j.perco !== 'error';
      setMsg((ok ? '✅ Проход по лицу убран: ' : '⚠️ По камере не распознаётся, но ') + j.name + ' - ' + parts.join('; '), ok ? 'good' : 'err');
      resetShot();
    }
    loadUsers(); refreshStatus();
  } catch (e) { setMsg('⚠️ ' + e, 'err'); }
  updateSend();
}

// ------------------------------------------------------------------ настройки
function setCfgMsg(text, cls) { const m = $('cfgMsg'); m.textContent = text; m.className = cls || 'hint'; }
async function loadConfigText() {
  try {
    const r = await fetch('/api/config');
    $('cfgText').value = await r.text();
    setCfgMsg(r.ok ? '' : '❌ не удалось прочитать конфиг', 'err');
  } catch (e) { setCfgMsg('⚠️ ' + e, 'err'); }
}
async function saveConfigText() {
  if (!confirm('Применить конфигурацию? Распознавание перезапустится на несколько секунд.')) return;
  $('cfgBtn').disabled = true; setCfgMsg('применяю…');
  try {
    const r = await fetch('/api/config', { method: 'PUT', headers: { 'Content-Type': 'text/plain; charset=utf-8' }, body: $('cfgText').value });
    const j = await r.json();
    setCfgMsg(r.ok ? '✅ Конфиг применён' : '❌ ' + j.detail, r.ok ? 'good' : 'err');
    refreshStatus(); loadUsers();
  } catch (e) { setCfgMsg('⚠️ ' + e, 'err'); }
  $('cfgBtn').disabled = false;
}
async function uploadConfig(event) {
  event.preventDefault();
  try {
    const r = await fetch('/config', { method: 'POST', body: new FormData($('configForm')) });
    const j = await r.json();
    alert(r.ok ? '✅ Конфиг применён: ' + j.filename : '❌ Ошибка: ' + j.detail);
    refreshStatus(); loadUsers(); loadConfigText();
  } catch (e) { alert('⚠️ Не удалось загрузить: ' + e); }
}
async function synchronize(event) {
  event.preventDefault();
  try {
    const r = await fetch('/sync', { method: 'POST' });
    const j = await r.json();
    alert(r.ok ? `✅ Синхронизировано: новых фото ${j.encoded}, без лица ${j.failed}, сотрудников ${j.users_total} (по камере ${j.users}), эмбеддингов ${j.embeddings}` : '❌ Ошибка: ' + j.detail);
    refreshStatus(); loadUsers();
  } catch (e) { alert('⚠️ ' + e); }
}

(async () => {
  await loadMe();                      // сначала узнаём уровень доступа - от него зависят вкладки и запросы
  showTab(initialTab(), false);
  setVideo(videoOn);
  refreshStatus();
  if (level >= 2) loadUsers();
  if (level >= 3) loadConfigText();
  loadEvents();
})();
setInterval(refreshStatus, 3000);

// ------------------------------------------------------------------ «Конфиг»: подвкладки «Настройки» и «Журнал log.txt»
function showSub(name) {
  ['cfg', 'log'].forEach(t => {
    $('sub-' + t).classList.toggle('hidden', t !== name);
    $('subBtn-' + t).classList.toggle('active', t === name);
  });
  try { localStorage.setItem('sub', name); } catch (e) {}
  if (name === 'log') loadLog();
}
async function loadLog() {
  $('logMsg').textContent = 'загрузка...';
  try {
    const r = await fetch('/api/log?errors=' + ($('logErrors').checked ? 1 : 0));
    const j = await r.json();
    if (!r.ok) { $('logMsg').textContent = j.detail || ('Ошибка ' + r.status); return; }
    $('logTitle').textContent = `Журнал за последние ${j.hours} ч`;
    const box = $('logBox');
    box.textContent = '';
    const frag = document.createDocumentFragment();
    j.lines.forEach(line => {
      const d = document.createElement('div');
      d.textContent = line;                                   // только текст, не HTML
      if (/^\S+ \S+ (ERROR|CRITICAL)\b/.test(line)) d.className = 'e';
      else if (/^\S+ \S+ WARNING\b/.test(line)) d.className = 'w';
      frag.appendChild(d);
    });
    box.appendChild(frag);
    box.scrollTop = box.scrollHeight;                         // свежие записи - внизу
    $('logMsg').textContent = `строк: ${j.lines.length}` + (j.truncated ? ' (показаны последние)' : '') + (j.error ? ' · ' + j.error : '');
  } catch (e) { $('logMsg').textContent = 'Нет связи с сервером'; }
}
$('logErrors').addEventListener('change', loadLog);
