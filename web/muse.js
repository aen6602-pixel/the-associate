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

const state = { sessionId: null, messages: [], sessions: [], channels: [], busy: false };

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
  $('ch-count').textContent = `(${state.channels.length}/${d.configured ?? 0})`;

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
    .map((c) => `<option value="${esc(c.id)}">${esc(c.id)} (${c.n})</option>`).join('');
  sel.value = keep;
  $('channel-list').innerHTML = state.channels.map((c) => `<div class="ch">
      <span>${esc(c.id)}</span><code>${c.n}건 · ${esc(String(c.last || '').slice(0, 10))}</code>
    </div>`).join('') || '<p class="hint">아직 모은 글이 없습니다.</p>';

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
function postsHtml(posts) {
  if (!posts || !posts.length) return '';
  return `<details class="trace"><summary>📡 근거 채널 글 (${posts.length}건)</summary>
    <div class="trace-body">${posts.map((p) => `<div class="trace-item">
      <div class="head"><span class="tier">📡 채널</span>
        <code>${esc(p.channel)}</code>
        <span class="val">${esc(String(p.date || '').slice(0, 10))}</span></div>
      <div class="meta">${esc(p.excerpt || '')}…</div></div>`).join('')}</div></details>`;
}

function render() {
  $('chat').innerHTML = state.messages.map((m) => (m.role === 'user'
    ? `<div class="msg user"><div class="avatar">You</div><div class="body">${esc(m.content)}</div></div>`
    : `<div class="msg assistant"><div class="avatar">M</div><div class="body">
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
  if (q) ask(q);
});

async function ask(question) {
  if (state.busy) return;
  state.busy = true;
  $('send-btn').disabled = true;
  box.value = '';
  box.style.height = 'auto';

  state.messages.push({ role: 'user', content: question });
  render();

  const live = document.createElement('div');
  live.className = 'msg assistant';
  live.innerHTML = `<div class="avatar">M</div><div class="body">
    <div class="hint"><span class="spinner"></span> 채널 글에서 찾는 중…</div></div>`;
  $('chat').appendChild(live);

  let posts = [];
  let text = '';
  let html = '';
  try {
    const res = await fetch('/api/muse/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question, session_id: state.sessionId, channel: $('channel-select').value || null,
      }),
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
        else if (ev.type === 'sources') posts = ev.posts || [];
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
    state.messages.push({ role: 'assistant', content: text, html, posts });
    state.busy = false;
    $('send-btn').disabled = false;
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
