/* The Associate — 프론트엔드.
 *
 * 빌드 스텝 없는 순수 JS(모듈/번들러 없음) — 배포가 `pip install + uvicorn` 하나로 끝나게 하려는
 * 의도적 선택이다. 마크다운 → HTML 변환은 서버(core/markdown.py)가 하고 여기서는 그대로 꽂는다
 * (웹 UI 와 HTML 리포트가 같은 렌더러를 쓰게 하려고).
 */
'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  sessionId: null,
  messages: [],      // [{role, content, trace, html}]
  sessions: [],
  provider: null,
  model: null,
  engines: [],
  busy: false,
};

const TIER = {
  authoritative: ['🟢 공식', '정부·규제기관·중앙은행·거래소'],
  reference: ['🔵 참조', '업계 표준 데이터셋(Damodaran 등)'],
  computed: ['🟣 계산', '엔진이 계산한 파생값'],
  assumption: ['🟠 가정', '사용자가 준 가정'],
  llm_estimate: ['🔴 LLM추정', '소스 없음 — 검증 필요'],
};

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmtArgs = (d) => Object.entries(d || {})
  .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ');

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* 본문 없음 */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

/* ── 로그인 ─────────────────────────────────────────── */
async function boot() {
  let me;
  try {
    me = await api('/api/me');
  } catch (e) {
    showGate(`서버에 연결할 수 없습니다: ${e.message}`, false);
    return;
  }
  if (me.blocked) { showGate(me.message, false, true); return; }
  if (!me.authenticated) { showGate(null, me.needs_name); return; }
  await enterApp();
}

/* fatal=true 는 "설정이 없어서 아무도 못 들어오는 상태" — 로그인해봐야 소용없으니 폼을 감춘다. */
function showGate(message, needsName, fatal = false) {
  $('shell').hidden = true;
  $('gate').hidden = false;
  $('name-row').hidden = !needsName;
  $('login-form').hidden = fatal;
  $('gate-hint').hidden = fatal;
  const box = $('gate-msg');
  box.hidden = !message;
  if (message) box.textContent = message;
  if (!message && !fatal) {
    setTimeout(() => $(needsName ? 'login-name' : 'login-password').focus(), 60);
  }
}

$('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = $('login-submit');
  btn.disabled = true;
  try {
    await api('/api/login', {
      method: 'POST',
      body: JSON.stringify({ name: $('login-name').value, password: $('login-password').value }),
    });
    $('gate').hidden = true;
    await enterApp();
  } catch (err) {
    const box = $('gate-msg');
    box.hidden = false;
    box.textContent = err.message;
    $('login-password').value = '';
    $('login-password').focus();
  } finally {
    btn.disabled = false;
  }
});

$('logout-btn').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST' });
  location.reload();
});

/* ── 앱 진입 ─────────────────────────────────────────── */
async function enterApp() {
  const data = await api('/api/bootstrap');
  $('gate').hidden = true;
  $('shell').hidden = false;

  $('viewer-row').hidden = !data.gate;
  $('viewer-label').textContent = `👤 ${data.viewer.label}`;
  $('admin-link').hidden = !data.viewer.is_admin;
  $('ephemeral-warn').hidden = !(data.deploy_mode && !data.persistent_storage);

  state.engines = data.engines;
  state.provider = data.default_engine.provider;
  const eng = state.engines.find((e) => e.provider === state.provider) || state.engines[0];
  state.model = eng ? eng.default_model : null;
  renderEngine();
  renderSources(data.sources, data.roadmap);
  renderSkills(data.skills || []);

  const s = await api('/api/sessions');
  state.sessions = s.sessions;
  const fresh = await api('/api/sessions/new', { method: 'POST' });
  state.sessionId = fresh.id;
  renderSessions();
  $('question').focus();
}

/* ── 엔진 선택 ───────────────────────────────────────── */
function renderEngine() {
  const eng = state.engines.find((e) => e.provider === state.provider);
  if (!eng) return;

  $('engine-summary').textContent =
    `🧠 Engine: ${eng.label} · ${state.model}  ${eng.connected ? '✅' : '⬜'}`;
  $('engine-box').open = !eng.connected;

  const ps = $('provider-select');
  ps.innerHTML = state.engines
    .map((e) => `<option value="${esc(e.provider)}"${e.provider === state.provider ? ' selected' : ''}>${esc(e.label)}</option>`)
    .join('');

  const CUSTOM = '__custom__';
  const ms = $('model-select');
  const known = eng.presets.includes(state.model);
  ms.innerHTML = eng.presets
    .map((m) => `<option value="${esc(m)}"${m === state.model ? ' selected' : ''}>${esc(m)}</option>`)
    .join('') + `<option value="${CUSTOM}"${known ? '' : ' selected'}>✏️ 직접 입력…</option>`;
  const custom = $('model-custom');
  custom.hidden = known;
  if (!known) custom.value = state.model || '';

  const keyBox = $('engine-key');
  keyBox.className = `engine-key ${eng.connected ? 'ok' : 'no'}`;
  keyBox.textContent = eng.connected
    ? `${eng.key_name} 연결됨`
    : `${eng.key_name} 를 설정해야 이 두뇌를 쓸 수 있어요.`;

  const warn = $('engine-warn');
  warn.hidden = eng.connected;
  if (!eng.connected) {
    warn.textContent = `${eng.key_name} 가 설정되지 않아 에이전트가 동작하지 않습니다. `
      + '(채팅에 키를 붙이지 말고 서버 환경변수에 넣어주세요)';
  }
  $('send-btn').disabled = !eng.connected || state.busy;
}

$('provider-select').addEventListener('change', (e) => {
  state.provider = e.target.value;
  const eng = state.engines.find((x) => x.provider === state.provider);
  state.model = eng.default_model;
  renderEngine();
});

$('model-select').addEventListener('change', (e) => {
  if (e.target.value === '__custom__') {
    $('model-custom').hidden = false;
    $('model-custom').focus();
  } else {
    state.model = e.target.value;
    renderEngine();
  }
});

$('model-custom').addEventListener('change', (e) => {
  const v = e.target.value.trim();
  if (v) { state.model = v; renderEngine(); }
});

/* ── 데이터 소스 ─────────────────────────────────────── */
function renderSources(srcs, roadmap) {
  const groups = [
    ['Connected', srcs.filter((s) => s.status === 'live')],
    ['Key needed', srcs.filter((s) => s.status === 'nokey')],
    ['Planned', srcs.filter((s) => s.status === 'planned')],
  ];
  const statusText = {
    live: '연결됨 — 지금 사용 가능',
    nokey: (s) => `환경변수에 ${s.key_attr}_API_KEY 를 넣으면 연결됩니다.`,
    planned: '아직 provider 미연동 (예정)',
  };

  let html = '';
  for (const [title, items] of groups) {
    if (!items.length) continue;
    html += `<div class="src-group"><h4>${title} (${items.length})</h4>`;
    for (const s of items) {
      const tierKo = s.tier === 'authoritative'
        ? '공식 (정부·중앙은행·거래소)' : '참조 (업계표준 데이터셋)';
      const msg = s.status === 'nokey' ? statusText.nokey(s) : statusText[s.status];
      html += `<details class="src">
        <summary><span>${s.tier_icon} ${esc(s.name)}</span><span class="badge">${esc(s.badge)}</span></summary>
        <div class="src-body">
          <p><strong>${esc(s.org)}</strong></p>
          <p class="hint">등급: ${s.tier_icon} ${tierKo}</p>
          <p><strong>제공:</strong> ${esc(s.provides)}</p>
          <p><strong>사용처:</strong> ${esc(s.used_by)}</p>
          <div class="status ${s.status}">${esc(msg)}</div>
          ${s.note ? `<p class="hint">${esc(s.note)}</p>` : ''}
          <p>🔗 <a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.url)}</a></p>
        </div></details>`;
    }
    html += '</div>';
  }

  html += `<div class="src-group"><h4>Roadmap (${roadmap.length})</h4>
    <p class="hint">Not yet wired up — data worth adding later, listed here for reference.</p>`;
  for (const r of roadmap) {
    html += `<details class="src"><summary><span>🗺️ ${esc(r.name)}</span></summary>
      <div class="src-body"><p><strong>${esc(r.org)}</strong></p>
      <p><strong>제공 예정:</strong> ${esc(r.provides)}</p></div></details>`;
  }
  html += '</div>';
  $('source-groups').innerHTML = html;
}

/* ── 절차서(playbook) ────────────────────────────────── */
function renderSkills(list) {
  $('skills-section').hidden = !list.length;
  if (!list.length) return;
  $('skill-list').innerHTML = list.map((s) => `
    <details class="src">
      <summary><span>📘 ${esc(s.name)}</span></summary>
      <div class="src-body">
        <p>${esc(s.description)}</p>
        ${s.references.length
          ? `<p class="hint">참조: ${s.references.map((r) => esc(r)).join(', ')}</p>` : ''}
      </div>
    </details>`).join('');
}

/* ── 대화 목록 ───────────────────────────────────────── */
function renderSessions() {
  const box = $('session-list');
  if (!state.sessions.length) {
    box.innerHTML = '<p class="hint">No conversations yet — send a question and it\'ll appear here.</p>';
    return;
  }
  box.innerHTML = state.sessions.map((s) => `
    <div class="session-row">
      <button class="name${s.id === state.sessionId ? ' active' : ''}" data-load="${esc(s.id)}"
              title="${esc(s.title)}">${s.id === state.sessionId ? '📍 ' : ''}${esc(s.title)}</button>
      <button class="del" data-del="${esc(s.id)}" title="삭제">🗑</button>
    </div>`).join('');

  box.querySelectorAll('[data-load]').forEach((b) => {
    b.onclick = () => loadSession(b.dataset.load);
  });
  box.querySelectorAll('[data-del]').forEach((b) => {
    b.onclick = async () => {
      await api(`/api/sessions/${encodeURIComponent(b.dataset.del)}`, { method: 'DELETE' });
      state.sessions = state.sessions.filter((s) => s.id !== b.dataset.del);
      if (b.dataset.del === state.sessionId) await newSession();
      else renderSessions();
    };
  });
}

async function loadSession(sid) {
  const rec = await api(`/api/sessions/${encodeURIComponent(sid)}`);
  state.sessionId = sid;
  state.messages = rec.messages;
  renderChat();
  renderSessions();
}

async function newSession() {
  const fresh = await api('/api/sessions/new', { method: 'POST' });
  state.sessionId = fresh.id;
  state.messages = [];
  renderChat();
  renderSessions();
  $('question').focus();
}

$('new-session').addEventListener('click', newSession);

/* ── 대화 렌더 ───────────────────────────────────────── */
function traceItemHtml(t) {
  const r = t.result || {};
  const args = esc(fmtArgs(t.input));
  if (!r.ok) {
    return `<div class="trace-item err"><div class="head"><code>${esc(t.name)}(${args})</code></div>
      <div class="meta">${esc(r.error || '실패')}</div></div>`;
  }
  const v = r.value || {};
  const p = v.provenance || {};
  const [badge] = TIER[p.source_type] || ['⚪ ?', ''];
  const meta = [];
  if (p.as_of) meta.push(`기준 ${esc(p.as_of)}`);
  if (p.original_field) meta.push(`필드 <code>${esc(p.original_field)}</code>`);
  meta.push(`출처 ${esc(p.source)}`);
  const extras = Object.values(v.extras || {}).map((x) =>
    `<div class="extra">↳ <strong>${esc(x.label)}</strong>: ${esc(
      typeof x.value === 'number' ? x.value.toLocaleString() : x.value)} ${esc(x.unit || '')}</div>`).join('');

  return `<div class="trace-item">
    <div class="head"><span class="tier">${badge}</span>
      <code>${esc(t.name)}(${args})</code>
      <span class="val">→ ${esc(v.value)} ${esc(v.unit || '')}</span></div>
    <div class="meta">${meta.join(' · ')}<br>${
      p.source_url ? `<a href="${esc(p.source_url)}" target="_blank" rel="noopener noreferrer">${esc(p.source_url)}</a>` : ''}</div>
    ${extras}</div>`;
}

function messageHtml(m, index) {
  if (m.role === 'user') {
    return `<div class="msg user"><div class="avatar">You</div>
      <div class="body">${esc(m.content)}</div></div>`;
  }
  const trace = m.trace || [];
  const hasDcf = trace.some((t) => t.name === 'compute_dcf' && (t.result || {}).ok);
  const exports = trace.length ? `<div class="exports">
      ${hasDcf ? `<button class="ghost sm" data-export="dcf_full" data-index="${index}">📥 전체 DCF 모델 (5시트)</button>` : ''}
      <button class="ghost sm" data-export="html_report" data-index="${index}">📄 HTML 리포트</button>
    </div>` : '';

  return `<div class="msg assistant"><div class="avatar">A</div><div class="body">
    ${trace.length ? `<details class="trace"><summary>🔍 사용한 데이터 소스 (${trace.length}개)</summary>
      <div class="trace-body">${trace.map(traceItemHtml).join('')}</div></details>` : ''}
    <div class="md">${m.html || esc(m.content)}</div>
    ${exports}</div></div>`;
}

function renderChat() {
  $('caps').hidden = state.messages.length > 0;
  $('chat').innerHTML = state.messages.map(messageHtml).join('');
  wireExports();
  scrollToEnd();
}

function wireExports() {
  document.querySelectorAll('[data-export]').forEach((b) => {
    b.onclick = async () => {
      const label = b.textContent;
      b.disabled = true;
      b.textContent = '생성 중…';
      try {
        await download(Number(b.dataset.index), b.dataset.export);
      } catch (e) {
        alert(`내보내기 실패: ${e.message}`);
      } finally {
        b.disabled = false;
        b.textContent = label;
      }
    };
  });
}

async function download(index, kind) {
  const res = await fetch(`/api/sessions/${encodeURIComponent(state.sessionId)}/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ index, kind }),
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* 본문 없음 */ }
    throw new Error(detail);
  }
  const disp = res.headers.get('Content-Disposition') || '';
  const m = /filename\*=UTF-8''([^;]+)/i.exec(disp);
  const name = m ? decodeURIComponent(m[1]) : (kind === 'dcf_full' ? 'model.xlsx' : 'report.html');
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}

function scrollToEnd() {
  const c = document.querySelector('.content');
  c.scrollTop = c.scrollHeight;
}

/* ── 질문 전송 (SSE 스트리밍) ────────────────────────── */
const composer = $('composer');
const questionBox = $('question');

questionBox.addEventListener('input', () => {
  questionBox.style.height = 'auto';
  questionBox.style.height = `${Math.min(questionBox.scrollHeight, window.innerHeight * 0.4)}px`;
});

questionBox.addEventListener('keydown', (e) => {
  // Enter 전송 / Shift+Enter 줄바꿈 (기존 챗 UX 유지)
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

composer.addEventListener('submit', async (e) => {
  e.preventDefault();
  const question = questionBox.value.trim();
  if (!question || state.busy) return;

  state.busy = true;
  $('send-btn').disabled = true;
  questionBox.value = '';
  questionBox.style.height = 'auto';

  state.messages.push({ role: 'user', content: question });
  renderChat();

  // 진행 중 표시 — 실제 tool 호출을 실시간으로 흘려보여준다.
  const live = document.createElement('div');
  live.className = 'msg assistant';
  live.innerHTML = `<div class="avatar">A</div><div class="body">
    <details class="trace" open><summary><span class="spinner"></span> 데이터 소스 조회 중…</summary>
    <div class="trace-body" id="live-trace"></div></details></div>`;
  $('chat').appendChild(live);
  scrollToEnd();
  const liveBody = $('live-trace');

  const append = (html) => {
    liveBody.insertAdjacentHTML('beforeend', html);
    scrollToEnd();
  };

  try {
    const res = await fetch('/api/ask', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question, session_id: state.sessionId,
        provider: state.provider, model: state.model,
      }),
    });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* 본문 없음 */ }
      throw new Error(detail);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let done = false;

    while (!done) {
      const { value, done: finished } = await reader.read();
      if (finished) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE 프레임은 빈 줄로 구분된다.
      let sep;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const line = frame.split('\n').find((l) => l.startsWith('data: '));
        if (!line) continue;

        const ev = JSON.parse(line.slice(6));
        if (ev.type === 'start') {
          state.sessionId = ev.session_id;
        } else if (ev.type === 'tool_use') {
          append(`<div class="trace-live">🔧 호출: ${esc(ev.name)}(${esc(fmtArgs(ev.input))})</div>`);
        } else if (ev.type === 'progress') {
          append(`<div class="trace-live">🔧 ${esc(ev.text)}</div>`);
        } else if (ev.type === 'tool_result') {
          append(traceItemHtml(ev));
        } else if (ev.type === 'error') {
          append(`<div class="trace-item err">${esc(ev.text)}</div>`);
        } else if (ev.type === 'final') {
          state.messages.push({
            role: 'assistant', content: ev.text, html: ev.html, trace: ev.trace || [],
          });
        } else if (ev.type === 'done') {
          state.sessionId = ev.session_id;
          state.sessions = ev.sessions || state.sessions;
          done = true;
        }
      }
    }
  } catch (err) {
    state.messages.push({
      role: 'assistant',
      content: `⚠️ ${err.message}`,
      html: `<p>⚠️ ${esc(err.message)}</p>`,
      trace: [],
    });
  } finally {
    live.remove();
    state.busy = false;
    renderChat();
    renderSessions();
    renderEngine();
    questionBox.focus();
  }
});

/* ── 사이드바 토글 (좁은 화면) ───────────────────────── */
$('sidebar-toggle').addEventListener('click', () => {
  $('sidebar').classList.toggle('collapsed');
});

/* ── 시작 ───────────────────────────────────────────── */
setTimeout(() => { const s = $('splash'); if (s) s.remove(); }, 2300);
boot();
