/* Market Muse — 구독 채널 브리핑.
 *
 * app.js 를 재사용하지 않고 따로 둔 이유: 본 채팅의 후처리(출처 각주·핵심수치 카드·
 * 검증 경고)는 **공시 기반 trace** 를 전제로 만들어졌다. 채널 전언에 그 장식을 그대로
 * 붙이면 같은 무게로 보여서, 두 데이터의 신뢰도 차이가 지워진다. 여기서는 근거를
 * '어느 채널의 며칠자 글' 로만 보여준다.
 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const state = {
  sessionId: null, messages: [], sessions: [],
  channels: [],        // 실제로 글이 쌓인 채널 (수집 결과)
  configured: [],      // 목록에 적힌 채널 (아직 못 읽은 것 포함)
  aliases: {},         // 채널 → 별칭
  canManage: false, busy: false,
};

/* 화면에는 `@jake8lee` 가 아니라 '잠실개미' 가 보여야 한다 — 40개를 아이디로 외우는
 * 사람은 없다. 별칭이 없을 때만 아이디를 그대로 쓴다. */
const chLabel = (id) => state.aliases[id] || id;

async function api(path, options = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* 본문 없음 */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ── 부팅 ─────────────────────────────────────────────── */
(async function boot() {
  let me;
  try { me = await api('/api/me'); } catch (e) { location.href = '/'; return; }
  if (!me.authenticated) { location.href = '/'; return; }   // 로그인 게이트는 본 앱이 담당
  $('viewer-label').textContent = `👤 ${me.label || ''}`;

  await loadStatus();
  await newSession();
  await loadSessions();
  $('question').focus();
})();

/* 수집 현황. 수집이 도는 중이면 끝날 때까지 주기적으로 다시 본다. */
let pollTimer = null;
async function loadStatus() {
  let d;
  try {
    d = await api('/api/muse/status');
  } catch (e) {
    $('snap-meta').textContent = e.message;
    $('snap-meta').className = 'warn';
    return;
  }
  state.channels = d.channels || [];
  state.configured = d.configured_channels || [];
  state.aliases = {};
  for (const c of state.configured) if (c.alias) state.aliases[c.id] = c.alias;
  state.canManage = !!d.can_manage;
  $('ch-add-form').hidden = !state.canManage;
  $('collect-btn').hidden = !state.canManage;
  $('ch-count').textContent =
    `(${state.channels.length}/${state.configured.length || d.configured || 0})`;

  const when = d.collected_at
    ? String(d.collected_at).slice(0, 16).replace('T', ' ') : '아직 수집 안 함';
  $('snap-meta').className = 'hint';
  $('snap-meta').textContent = d.running
    ? `수집 중… ${d.note || ''}`
    : `최근 ${d.lookback_days}일 · ${(d.count || 0).toLocaleString()}건 · ${when}`;
  if (d.last_error) {
    $('snap-meta').className = 'warn';
    $('snap-meta').textContent += `\n⚠︎ ${d.last_error}`;
  }

  // 채널 선택 목록 — 실제로 글이 쌓인 채널만 고를 수 있게 한다.
  const sel = $('channel-select');
  const keep = sel.value;
  sel.innerHTML = '<option value="">전체 채널</option>' + state.channels
    .map((c) => `<option value="${esc(c.id)}">${esc(chLabel(c.id))} (${c.n})</option>`).join('');
  sel.value = keep;

  // 아직 한 건도 못 읽은 채널도 함께 보여준다 — 방금 추가한 채널이 목록에서 빠지면
  // 추가가 실패한 줄 알게 된다.
  const collected = new Map(state.channels.map((c) => [c.id, c]));
  const rows = state.configured.map(
    (c) => ({ ...c, ...(collected.get(c.id) || { n: 0, last: '' }) }));
  for (const c of state.channels) if (!rows.some((r) => r.id === c.id)) rows.push({ ...c });
  rows.sort((a, b) => (b.n || 0) - (a.n || 0));

  $('channel-list').innerHTML = rows.map((c) => `<div class="ch">
      <span title="${esc(c.id)}">${esc(chLabel(c.id))}</span>
      <code>${c.n ? `${c.n}건 · ${esc(String(c.last || '').slice(0, 10))}` : '아직 없음'}</code>
      ${state.canManage ? `<button type="button" class="ch-del" data-del="${esc(c.id)}"
          title="목록에서 빼고 모아둔 글도 지웁니다">×</button>` : ''}
    </div>`).join('') || '<p class="hint">채널 목록이 비어 있습니다.</p>';

  for (const b of document.querySelectorAll('[data-del]')) {
    b.onclick = async () => {
      const id = b.dataset.del;
      if (!confirm(`'${chLabel(id)}' 를 목록에서 뺄까요?\n모아둔 글도 함께 지워집니다.`)) return;
      b.disabled = true;
      try {
        await api(`/api/muse/channels/${encodeURIComponent(id)}`, { method: 'DELETE' });
        await loadStatus();
      } catch (e) { alert(e.message); b.disabled = false; }
    };
  }

  clearTimeout(pollTimer);
  if (d.running || d.refresh_started) pollTimer = setTimeout(loadStatus, 3000);
}

$('collect-btn').addEventListener('click', async () => {
  const b = $('collect-btn');
  b.disabled = true;
  try {
    await api('/api/muse/collect', { method: 'POST' });
    await loadStatus();
  } catch (e) {
    alert(e.message);
  } finally {
    b.disabled = false;
  }
});

$('ch-add-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const channel = $('ch-add-id').value.trim();
  if (!channel) return;
  const btn = e.target.querySelector('button');
  btn.disabled = true;
  try {
    await api('/api/muse/channels', {
      method: 'POST',
      body: JSON.stringify({ channel, alias: $('ch-add-alias').value.trim() }),
    });
    $('ch-add-id').value = '';
    $('ch-add-alias').value = '';
    await loadStatus();   // 추가 직후 그 채널만 뒤에서 읽는 중 — 폴링이 붙는다
  } catch (err) {
    alert(err.message);
  } finally {
    btn.disabled = false;
  }
});

async function newSession() {
  const fresh = await api('/api/sessions/new', { method: 'POST' });
  state.sessionId = fresh.id;
  state.messages = [];
  render();
}

async function loadSessions() {
  try {
    state.sessions = (await api('/api/muse/sessions')).sessions || [];
  } catch (_) { state.sessions = []; }
  renderSessions();
}

function renderSessions() {
  $('session-list').innerHTML = state.sessions.length
    ? state.sessions.map((s) => `<div class="session-row">
        <button class="name${s.id === state.sessionId ? ' active' : ''}"
                data-open="${esc(s.id)}">${esc(s.title || '새 대화')}</button></div>`).join('')
    : '<p class="hint">아직 대화가 없습니다.</p>';
  for (const b of document.querySelectorAll('[data-open]')) {
    b.onclick = async () => {
      const rec = await api(`/api/muse/sessions/${encodeURIComponent(b.dataset.open)}`);
      state.sessionId = rec.id;
      state.messages = rec.messages || [];
      render();
      renderSessions();
    };
  }
}

$('new-session').addEventListener('click', () => { newSession(); loadSessions(); });

/* ── 렌더 ─────────────────────────────────────────────── */
/* 어느 채널만 봤는지는 답변 자체만큼 중요하다 — 한 채널로 좁혀 놓고 '시장 전체가
 * 이렇다' 로 읽으면 곤란하다. */
function scopeHtml(scope) {
  if (!scope || !scope.channel) return '';
  const how = scope.auto ? '질문에서 채널 이름을 알아봤습니다' : '채널을 한정해 찾았습니다';
  return `<div class="muse-scope">🎯 <strong>${esc(scope.label || scope.channel)}</strong>
    채널 글에서만 찾았습니다 <span class="hint">— ${esc(how)}</span></div>`;
}

function postsHtml(posts) {
  if (!posts || !posts.length) return '';
  return `<details class="trace"><summary>📡 근거 채널 글 (${posts.length}건)</summary>
    <div class="trace-body">${posts.map((p) => `<div class="trace-item">
      <div class="head"><span class="tier">📡 채널</span>
        <code title="${esc(p.channel)}">${esc(p.alias || chLabel(p.channel))}</code>
        <span class="val">${esc(String(p.date || '').slice(0, 10))}</span></div>
      <div class="meta">${esc(p.excerpt || '')}…</div></div>`).join('')}</div></details>`;
}

function render() {
  $('chat').innerHTML = state.messages.map((m) => (m.role === 'user'
    ? `<div class="msg user"><div class="avatar">You</div><div class="body">${esc(m.content)}</div></div>`
    : `<div class="msg assistant"><div class="avatar">M</div><div class="body">
        ${scopeHtml(m.scope)}
        ${postsHtml(m.posts)}
        <div class="md">${m.html || esc(m.content)}</div>
        <div class="muse-foot">📡 구독 채널 전언 — 공시로 확인되지 않은 내용입니다.</div>
      </div></div>`)).join('');
  const c = document.querySelector('.content');
  c.scrollTop = c.scrollHeight;
}

/* ── 전송 ─────────────────────────────────────────────── */
const composer = $('composer');
const box = $('question');

box.addEventListener('input', () => {
  box.style.height = 'auto';
  box.style.height = `${Math.min(box.scrollHeight, window.innerHeight * 0.4)}px`;
});
box.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); composer.requestSubmit(); }
});
composer.addEventListener('submit', (e) => {
  e.preventDefault();
  const q = box.value.trim();
  if (!q) return;
  box.value = '';
  box.style.height = 'auto';
  run('/api/muse/ask', { question: q, channel: $('channel-select').value || null },
      q, '채널 글에서 찾는 중…');
});

$('brief-btn').addEventListener('click', () => {
  const ch = $('channel-select').value || null;
  const where = ch ? `'${chLabel(ch)}' 채널` : '구독 채널 전체';
  run('/api/muse/brief', { channel: ch }, `📋 ${where}의 최근 흐름 브리핑`,
      '최근 글을 주제별로 묶는 중… (수백 건이라 조금 걸립니다)');
});

/* 질문과 브리핑은 같은 스트림 모양이라 한 함수로 받는다. */
async function run(path, body, userLabel, waiting) {
  if (state.busy) return;
  state.busy = true;
  $('send-btn').disabled = true;
  $('brief-btn').disabled = true;

  state.messages.push({ role: 'user', content: userLabel });
  render();

  const live = document.createElement('div');
  live.className = 'msg assistant';
  live.innerHTML = `<div class="avatar">M</div><div class="body">
    <div class="hint"><span class="spinner"></span> ${esc(waiting)}</div></div>`;
  $('chat').appendChild(live);

  let posts = [];
  let scope = null;
  let text = '';
  let html = '';
  try {
    const res = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, session_id: state.sessionId }),
    });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* 본문 없음 */ }
      throw new Error(detail);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    let done = false;
    while (!done) {
      const { value, done: fin } = await reader.read();
      if (fin) break;
      buf += dec.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf('\n\n')) !== -1) {
        const line = buf.slice(0, sep).trim();
        buf = buf.slice(sep + 2);
        if (!line.startsWith('data: ')) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.type === 'start') state.sessionId = ev.session_id;
        else if (ev.type === 'scope') {
          scope = { channel: ev.channel, label: ev.label, auto: ev.auto };
          live.querySelector('.body').insertAdjacentHTML('afterbegin', scopeHtml(scope));
        } else if (ev.type === 'sources') posts = ev.posts || [];
        else if (ev.type === 'final') text = ev.text || '';
        else if (ev.type === 'final_html') html = ev.html || '';
        else if (ev.type === 'error') text = `⚠️ ${ev.text}`;
        else if (ev.type === 'done') { state.sessions = ev.sessions || state.sessions; done = true; }
      }
    }
  } catch (err) {
    text = `⚠️ ${err.message}`;
  } finally {
    live.remove();
    state.messages.push({ role: 'assistant', content: text, html, posts, scope });
    state.busy = false;
    $('send-btn').disabled = false;
    $('brief-btn').disabled = false;
    render();
    renderSessions();
    box.focus();
  }
}

/* 좁은 화면 사이드바 */
$('sidebar-toggle').addEventListener('click', () => {
  $('sidebar').classList.toggle('open');
  $('sidebar-backdrop').hidden = !$('sidebar').classList.contains('open');
});
$('sidebar-backdrop').addEventListener('click', () => {
  $('sidebar').classList.remove('open');
  $('sidebar-backdrop').hidden = true;
});
