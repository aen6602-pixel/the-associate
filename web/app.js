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
  reasoning: null,
  reasoningLabels: {},
  engines: [],
  busy: false,
};

const TIER = {
  authoritative: ['🟢 공식', '정부·규제기관·중앙은행·거래소'],
  parsed_authoritative: ['🟢 원문', '공시 원문에서 직접 읽음 — 문서ID·인용 있음'],
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

/* ── 테마 ────────────────────────────────────────────── */
$('theme-btn').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem('assoc-theme', next); } catch (_) { /* 저장 실패해도 전환은 된다 */ }
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
  state.reasoningLabels = data.reasoning_labels || {};
  state.provider = data.default_engine.provider;
  const eng = state.engines.find((e) => e.provider === state.provider) || state.engines[0];
  state.model = eng ? eng.default_model : null;
  state.reasoning = eng ? eng.default_reasoning : null;
  renderEngine();
  renderSources(data.sources, data.roadmap);
  renderSkills(data.skills || []);

  const s = await api('/api/sessions');
  state.sessions = s.sessions;
  const fresh = await api('/api/sessions/new', { method: 'POST' });
  state.sessionId = fresh.id;
  renderSessions();
  $('question').focus();

  // 화면을 다 그린 뒤 확인한다 — await 하지 않아야 첫 화면이 늦어지지 않는다.
  loadHealth();
}

/* ── 엔진 선택 ───────────────────────────────────────── */
function renderEngine() {
  const eng = state.engines.find((e) => e.provider === state.provider);
  if (!eng) return;

  const effortTag = state.reasoning ? ` · 추론 ${state.reasoning}` : '';
  $('engine-summary').textContent =
    `🧠 Engine: ${eng.label} · ${state.model}${effortTag}  ${eng.connected ? '✅' : '⬜'}`;
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

  // 추론 강도 — provider 가 노브를 지원할 때만 보인다.
  const levels = eng.reasoning_levels || [];
  const rrow = $('reasoning-row');
  const rsel = $('reasoning-select');
  const rhint = $('reasoning-hint');
  rrow.hidden = levels.length === 0;
  rhint.hidden = levels.length === 0;
  if (levels.length) {
    if (!levels.includes(state.reasoning)) state.reasoning = eng.default_reasoning;
    rsel.innerHTML = levels
      .map((l) => {
        const label = state.reasoningLabels[l] || l;
        const dflt = l === eng.default_reasoning ? ' (기본)' : '';
        return `<option value="${esc(l)}"${l === state.reasoning ? ' selected' : ''}>${esc(label)}${dflt}</option>`;
      })
      .join('');
    rhint.textContent = '높이면 도구를 더 꼼꼼히 골라 다단계 밸류에이션에 유리하고, '
      + '낮추면 단순 조회가 빨라집니다. 숫자는 어느 강도에서도 도구가 만듭니다.';
  } else {
    state.reasoning = null;
  }

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
  // 추론강도 어휘가 provider 마다 달라(gemini 는 dynamic, openai 는 minimal) 그대로 들고
  // 넘어가면 서버가 400 을 낸다 → 새 provider 의 기본값으로 리셋한다.
  state.reasoning = eng.default_reasoning;
  renderEngine();
});

$('reasoning-select').addEventListener('change', (e) => {
  state.reasoning = e.target.value;
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
      html += `<details class="src" data-src="${esc(s.name)}">
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
  wireAccordion();
}

/* 소스 목록은 항목이 20개가 넘어서, 열어본 것이 전부 펼쳐진 채 남으면 사이드바가 금방
 * 길어지고 원하는 항목을 다시 찾기 어려워진다. 한 번에 하나만 열리게 한다. */
function wireAccordion() {
  const items = [...document.querySelectorAll('#source-groups details.src')];
  for (const d of items) {
    d.addEventListener('toggle', () => {
      if (!d.open) return;
      for (const other of items) {
        if (other !== d) other.open = false;
      }
    });
  }
}

/* ── 소스 실측 점검 ───────────────────────────────────────
 * 사이드바의 '연결' 표시는 원래 **환경변수에 키가 있는지**만 봤다. EDINET 이 API 호스트를
 * 옮겨 일본 조회가 통째로 죽은 동안에도 계속 '✅ 연결' 이었다(2026-08 실측). 그래서 실제
 * 응답을 확인한 결과로 덮어쓴다. bootstrap 과 분리한 이유는 전 소스를 두드리는 데 수 초가
 * 걸려 첫 화면이 그만큼 늦어지기 때문 — 화면을 먼저 그리고 결과가 오면 갱신한다.
 */
const HEALTH_BADGE = {
  up: ['✅ 정상', 'live'],
  down: ['❌ 응답없음', 'down'],
  nokey: ['⬜ 키 필요', 'nokey'],
  planned: ['🔜 예정', 'planned'],
};

function applyHealth(snap) {
  for (const r of snap.sources || []) {
    const el = document.querySelector(`.src[data-src="${CSS.escape(r.name)}"]`);
    if (!el) continue;   // 카탈로그에 행이 없는 소스(MOPS 등)는 표시할 자리가 없다
    const [label, cls] = HEALTH_BADGE[r.state] || HEALTH_BADGE.down;
    const badge = el.querySelector('.badge');
    if (badge) badge.textContent = label;
    const box = el.querySelector('.status');
    if (box) {
      box.className = `status ${cls}`;
      box.textContent = r.state === 'up'
        ? `${r.detail} · 응답 ${r.ms}ms`
        : r.detail;
    }
    // 죽은 소스를 펼쳐두지는 않는다 — 아코디언(한 번에 하나)과 충돌해서 여러 개가
    // 죽으면 마지막 하나만 남고, 무엇보다 사이드바가 열린 채로 길어진다.
    // 어느 소스가 죽었는지는 아래 요약 바가 이름으로 알려주고, 배지가 ❌ 로 바뀐다.
  }

  const down = snap.down || [];
  const bar = $('health-bar');
  bar.hidden = false;
  bar.className = `health-bar ${down.length ? 'bad' : 'ok'}`;
  const when = new Date((snap.checked_at || 0) * 1000);
  const hhmm = Number.isFinite(when.getTime())
    ? `${String(when.getHours()).padStart(2, '0')}:${String(when.getMinutes()).padStart(2, '0')}` : '';
  $('health-msg').textContent = down.length
    ? `⚠︎ ${down.join(', ')} 응답 없음 — 이 소스가 필요한 질문은 실패합니다`
    : `모든 소스 정상 · ${hhmm} 확인`;
}

async function loadHealth(refresh = false) {
  const btn = $('health-refresh');
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = '확인 중…';
  try {
    applyHealth(await api(`/api/health/sources${refresh ? '?refresh=true' : ''}`));
  } catch (e) {
    const bar = $('health-bar');
    bar.hidden = false;
    bar.className = 'health-bar bad';
    $('health-msg').textContent = `점검 실패: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

$('health-refresh').addEventListener('click', () => loadHealth(true));

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
  // 그 답변에서 실제로 성공한 계산 도구에 대응하는 엑셀만 제안한다 —
  // 계산하지 않은 방법의 버튼을 띄우면 눌러도 400 이 나서 혼란만 준다.
  const ran = (tool) => trace.some((t) => t.name === tool && (t.result || {}).ok);
  const xlsx = [
    ['compute_dcf', 'dcf_full', '📥 DCF 전체 모델 (5시트)'],
    ['compute_dcf', 'dcf', '📥 DCF 요약 (1시트)'],
    ['compute_comps', 'comps', '📥 Comps 엑셀'],
    ['evaluate_sangjeung_value', 'sangjeung', '📥 상증법 평가 엑셀'],
  ].filter(([tool]) => ran(tool));

  // 이 답변에 아직 답해야 할 decision 블록이 남아있으면(사용자가 선택지를 눌러야
  // 다음 단계로 넘어가는 중간 확인 단계) 최종 결과가 아니다 — 여기서 엑셀/리포트
  // 버튼을 보여주면 확정 전 가정으로 내보내게 된다. 결정이 없는(=최종) 답변에만 붙인다.
  const hasPendingDecision = (m.html || '').includes('class="decision"');
  const exports = (trace.length && !hasPendingDecision) ? `<div class="exports">
      ${xlsx.map(([, kind, label]) =>
        `<button class="ghost sm" data-export="${kind}" data-index="${index}">${label}</button>`).join('')}
      <button class="ghost sm" data-export="html_report" data-index="${index}">📄 HTML 리포트</button>
      <button class="ghost sm" data-copy="answer" data-index="${index}">📋 답변 복사</button>
    </div>` : '';

  return `<div class="msg assistant" data-idx="${index}"><div class="avatar">A</div><div class="body">
    ${trace.length ? `<details class="trace"><summary>🔍 사용한 데이터 소스 (${trace.length}개)</summary>
      <div class="trace-body">${trace.map(traceItemHtml).join('')}</div></details>` : ''}
    <div class="md">${m.html || esc(m.content)}</div>
    ${exports}</div></div>`;
}

/* ── 진행 단계 ───────────────────────────────────────────
 * 도구 호출이 흘러가기만 하면 "지금 뭘 하는 중인지" 를 알 수 없어 대기가 길게 느껴진다.
 * 도구 이름을 단계로 접어 어디까지 왔는지 보여준다. */
const STEPS = [
  { label: '데이터 수집', tools: /^(get_financial|get_market_cap|get_ebitda|search_|read_|get_figi|get_business_mix|get_mops)/ },
  { label: '가정·자본비용', tools: /^(get_dcf_assumptions|get_net_debt|get_cost_of_debt|get_market_cost_of_debt|get_effective_tax|get_terminal_growth|get_beta|get_industry_benchmarks|compute_wacc|get_equity_risk|get_country_risk|get_corporate_tax|get_risk_free|get_fx)/ },
  { label: '계산', tools: /^(compute_dcf|compute_scenarios|compute_comps|evaluate_sangjeung|diagnose_implied)/ },
  { label: '정리', tools: /^$/ },
];

function stepOf(toolName) {
  const i = STEPS.findIndex((s) => s.tools.test(toolName || ''));
  return i < 0 ? 0 : i;
}

// 되돌아가지 않는다 — 계산 뒤에 보조 조회를 한 번 더 해도 단계가 뒤로 밀리면 혼란스럽다.
let stepAt = -1;
function markStep(n) {
  if (n <= stepAt) return;
  stepAt = n;
  const box = $('live-steps');
  if (!box) return;
  for (const el of box.querySelectorAll('.step')) {
    const i = Number(el.dataset.step);
    el.classList.toggle('done', i < n);
    el.classList.toggle('at', i === n);
  }
}

/* ── 답변 후처리: 근거를 눈에 보이게 ─────────────────────
 * 아래 모든 것은 **trace(구조화된 도구 결과)** 에서만 만든다. LLM 이 쓴 문장을 파싱해
 * "이 숫자는 아마 저기서 왔겠지" 라고 추론하지 않는다 — 그 순간 오귀속이 생기고, 그건
 * 이 앱이 존재 이유로 삼는 원칙을 정면으로 깨는 일이다. 그래서 커버리지가 부분적이더라도
 * **정확히 일치하는 것만** 표시한다.
 */

// 계산 도구 → 그 답변의 헤드라인 숫자. 결론 수치가 표 안에 묻히지 않게 카드로 올린다.
const HEADLINE = {
  compute_dcf: 'DCF 주당가치',
  compute_scenarios: 'DCF 시나리오',
  evaluate_sangjeung_value: '상증법 주당 평가액',
  compute_wacc_auto: 'WACC',
  compute_wacc: 'WACC',
  get_market_cap: '시가총액',
  diagnose_implied_assumptions: '목표가 역산',
};

const CALC_METHOD = {
  compute_dcf: 'DCF (UFCF)',
  compute_scenarios: 'DCF 시나리오 (Base/Bull/Bear)',
  compute_comps: 'Trading Comps',
  evaluate_sangjeung_value: '상증법 보충적 평가',
  diagnose_implied_assumptions: '역산 진단 (Reverse DCF)',
};

const okItems = (trace) => trace.filter((t) => (t.result || {}).ok && (t.result.value || {}).value !== undefined);

function fmtNum(v, unit) {
  if (typeof v !== 'number') return String(v ?? '');
  const abs = Math.abs(v);
  // 조·억 단위로 접어 읽기 쉽게. 통화가 아닌 %·배는 그대로 둔다.
  if (['%', '배', '개', ''].includes(unit || '')) return v.toLocaleString('ko-KR');
  if (abs >= 1e12) return `${(v / 1e12).toLocaleString('ko-KR', { maximumFractionDigits: 2 })}조`;
  if (abs >= 1e8) return `${(v / 1e8).toLocaleString('ko-KR', { maximumFractionDigits: 1 })}억`;
  return v.toLocaleString('ko-KR');
}

/* 값 하나가 답변 본문에 어떤 문자열로 적혔을지 후보를 만든다.
   맞히려고 넓게 잡지 않는다 — 짧은 숫자는 우연히 일치하기 쉬워 4자 미만은 아예 버린다. */
function matchStrings(v, unit) {
  if (typeof v !== 'number' || !Number.isFinite(v)) return [];
  const out = new Set([
    v.toLocaleString('ko-KR'),
    v.toLocaleString('en-US'),
    String(v),
  ]);
  if (!Number.isInteger(v)) {
    out.add(v.toFixed(2));
    out.add(v.toFixed(4).replace(/0+$/, '').replace(/\.$/, ''));
  }
  if (unit === '%') { out.add(`${v}%`); out.add(`${v.toFixed(2)}%`); }
  return [...out].filter((s) => s.length >= 4);
}

function collectFacts(trace) {
  const facts = [];       // {strings, tier, source, url, as_of, label}
  const push = (v, toolName) => {
    const p = v.provenance || {};
    const strings = matchStrings(v.value, v.unit);
    if (strings.length) {
      facts.push({ strings, tier: p.source_type, source: p.source, url: p.source_url,
                   as_of: p.as_of, label: v.label || toolName });
    }
    for (const x of Object.values(v.extras || {})) push(x, toolName);
  };
  for (const t of okItems(trace)) push(t.result.value, t.name);
  return facts;
}

// 엔진이 붙인 ⚠️ 경고. **LLM 이 옮겨 적었는지와 무관하게** 여기서 직접 꺼내 보여준다 —
// 옮겨 적기를 프롬프트로 지시해 두었지만, 지시가 지켜졌는지에 신뢰를 걸 이유가 없다.
function collectWarnings(trace) {
  const out = [];
  const scan = (v, toolName) => {
    const note = (v.provenance || {}).note || '';
    for (const line of note.split(/(?=⚠️)/)) {
      const s = line.trim();
      if (s.startsWith('⚠️') && s.length > 4) out.push({ tool: toolName, text: s.replace(/^⚠️\s*/, '') });
    }
    for (const x of Object.values(v.extras || {})) scan(x, toolName);
  };
  for (const t of okItems(trace)) scan(t.result.value, t.name);
  // 같은 경고가 extras 를 타고 중복되기 쉬우므로 문구 기준으로 접는다.
  const seen = new Set();
  return out.filter((w) => !seen.has(w.text) && seen.add(w.text));
}

function collectSources(trace) {
  const by = new Map();
  const scan = (v) => {
    const p = v.provenance || {};
    if (p.source && !by.has(p.source)) {
      by.set(p.source, { source: p.source, tier: p.source_type, url: p.source_url, as_of: p.as_of });
    }
    for (const x of Object.values(v.extras || {})) scan(x);
  };
  for (const t of okItems(trace)) scan(t.result.value);
  return [...by.values()];
}

function headerHtml(trace) {
  const items = okItems(trace);
  if (!items.length) return '';
  // 회사명은 도구 인자에서 가장 많이 등장한 것을 쓴다(추론이 아니라 실제로 조회한 대상).
  const counts = {};
  for (const t of trace) {
    const c = (t.input || {}).company;
    if (c) counts[c] = (counts[c] || 0) + 1;
  }
  const company = Object.keys(counts).sort((a, b) => counts[b] - counts[a])[0];
  const methods = [...new Set(trace.map((t) => CALC_METHOD[t.name]).filter(Boolean))];
  const asOf = items.map((t) => (t.result.value.provenance || {}).as_of).filter(Boolean);
  if (!company && !methods.length) return '';

  const bits = [];
  if (company) bits.push(`<span class="k">대상</span><span class="v">${esc(company)}</span>`);
  if (methods.length) bits.push(`<span class="k">방법</span><span class="v">${esc(methods.join(' · '))}</span>`);
  if (asOf.length) bits.push(`<span class="k">기준</span><span class="v">${esc([...new Set(asOf)].slice(0, 3).join(', '))}</span>`);
  bits.push(`<span class="k">조회</span><span class="v">${new Date().toLocaleDateString('ko-KR')}</span>`);
  return `<div class="ans-head">${bits.join('')}</div>`;
}

function figuresHtml(trace) {
  const cards = [];
  for (const t of okItems(trace)) {
    const title = HEADLINE[t.name];
    if (!title) continue;
    const v = t.result.value;
    if (v.value === null) continue;   // 봉인된 결과(NM)는 카드로 만들지 않는다
    cards.push(`<div class="fig"><div class="fig-k">${esc(title)}</div>
      <div class="fig-v">${esc(fmtNum(v.value, v.unit))}<span class="fig-u">${esc(v.unit || '')}</span></div>
      <div class="fig-l">${esc(v.label || '')}</div></div>`);
  }
  return cards.length ? `<div class="figs">${cards.join('')}</div>` : '';
}

function warningsHtml(trace) {
  const ws = collectWarnings(trace);
  if (!ws.length) return '';
  return `<details class="warn-banner" open>
    <summary>⚠️ 검증 경고 ${ws.length}건 — 결론보다 먼저 확인하세요</summary>
    <ul>${ws.map((w) => `<li><code>${esc(w.tool)}</code> ${esc(w.text)}</li>`).join('')}</ul>
  </details>`;
}

function sourcesHtml(trace) {
  const list = collectSources(trace);
  if (!list.length) return '';
  const li = list.map((s, n) => {
    const [badge] = TIER[s.tier] || ['⚪ ?'];
    const link = s.url && /^https?:/.test(s.url)
      ? `<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.source)}</a>`
      : esc(s.source);
    return `<li id="src-${n + 1}"><span class="sn">${n + 1}</span>
      <span class="tier">${badge}</span> ${link}
      ${s.as_of ? `<span class="asof">${esc(s.as_of)}</span>` : ''}</li>`;
  }).join('');
  return `<div class="src-notes"><div class="src-notes-h">출처</div><ol>${li}</ol></div>`;
}

/* 본문 텍스트에 등장하는 값에 출처 등급 점을 단다.
   **텍스트 노드만** 만지므로 링크·태그 구조가 깨지지 않고, 값이 문자열로 정확히 일치할 때만
   붙인다(짐작해서 붙이지 않는다). 그래서 커버리지는 부분적이며, 그게 의도다. */
function annotateFacts(root, facts) {
  if (!facts.length) return;
  const pairs = [];
  for (const f of facts) for (const s of f.strings) pairs.push([s, f]);
  pairs.sort((a, b) => b[0].length - a[0].length);   // 긴 문자열 먼저 — 부분일치 방지

  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const queue = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (!n.parentElement.closest('a, code, .ans-head, .figs, .src-notes, .warn-banner')) queue.push(n);
  }
  const used = new Set();
  // 큐로 도는 이유: 한 문단에 값이 여러 개 있을 수 있다. 노드를 쪼갠 뒤 남은 뒷부분을
  // 큐에 다시 넣어야 두 번째 값부터도 잡힌다(그러지 않으면 문단마다 하나만 표시된다).
  while (queue.length) {
    const node = queue.shift();
    if (!node.nodeValue) continue;
    const hit = pairs.find(([s]) => !used.has(s) && node.nodeValue.includes(s));
    if (!hit) continue;
    const [s, f] = hit;
    used.add(s);
    const after = node.splitText(node.nodeValue.indexOf(s));
    const rest = after.splitText(s.length);
    const [badge, desc] = TIER[f.tier] || ['⚪ ?', ''];
    const mark = document.createElement('span');
    mark.className = `fact t-${f.tier || 'unknown'}`;
    mark.title = `${badge} · ${f.label || ''} · ${f.source || ''}`.trim() + (desc ? `\n${desc}` : '');
    mark.textContent = after.nodeValue;
    after.replaceWith(mark);
    queue.push(rest);
  }
}

/* 답변 안의 "(출처: …)" 를 위첨자 번호로 접는다. 아래 출처 목록의 이름과 **앞부분이
   일치할 때만** 번호를 매기고, 못 찾으면 원문 그대로 둔다 — 틀린 번호를 붙이느니
   원문이 낫다. */
function linkCitations(root, sources) {
  if (!sources.length) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (/\(출처\s*:/.test(n.nodeValue) && !n.parentElement.closest('a, code')) nodes.push(n);
  }
  const norm = (s) => s.toLowerCase().replace(/[\s()·,]/g, '');
  for (const node of nodes) {
    const frag = document.createDocumentFragment();
    let rest = node.nodeValue;
    let m;
    const re = /\(출처\s*:\s*([^)]+)\)/;
    while ((m = re.exec(rest))) {
      frag.append(rest.slice(0, m.index));
      const cited = norm(m[1]);
      const i = sources.findIndex((s) => cited.startsWith(norm(s.source).slice(0, 6)));
      if (i >= 0) {
        const a = document.createElement('a');
        a.className = 'cite';
        a.href = `#src-${i + 1}`;
        a.textContent = String(i + 1);
        a.title = m[1];
        frag.append(a);
      } else {
        frag.append(m[0]);       // 매칭 실패 → 원문 유지
      }
      rest = rest.slice(m.index + m[0].length);
    }
    frag.append(rest);
    node.replaceWith(frag);
  }
}

function decorateAnswers() {
  for (const el of document.querySelectorAll('.msg.assistant[data-idx]')) {
    if (el.dataset.decorated) continue;
    const m = state.messages[Number(el.dataset.idx)];
    const trace = (m && m.trace) || [];
    if (!trace.length) { el.dataset.decorated = '1'; continue; }
    const md = el.querySelector('.md');
    if (!md) continue;

    md.insertAdjacentHTML('beforebegin', headerHtml(trace) + warningsHtml(trace) + figuresHtml(trace));
    md.insertAdjacentHTML('afterend', sourcesHtml(trace));
    annotateFacts(md, collectFacts(trace));
    linkCitations(md, collectSources(trace));
    el.dataset.decorated = '1';
  }
}

function renderChat() {
  $('caps').hidden = state.messages.length > 0;
  $('chat').innerHTML = state.messages.map(messageHtml).join('');
  decorateAnswers();
  wireExports();
  wireCopy();
  wireDecisions();
  scrollToEnd();
}

/* ── 의사결정 카드: 클릭해서 고르고 한 번에 전송 ────────
 * 예전엔 답변에 "1A, 2A, 3A 처럼 답해 주세요" 가 있고 사용자가 직접 타이핑해야 했다.
 * 이제 마지막 assistant 메시지의 카드들을 클릭하면 선택이 모여 그대로 전송된다.
 */
function wireDecisions() {
  const blocks = [...document.querySelectorAll('.msg.assistant')];
  const last = blocks[blocks.length - 1];
  if (!last) return;
  const cards = [...last.querySelectorAll('.decision')];
  if (!cards.length) return;

  const picked = new Map();   // decision id -> "1A"

  const bar = document.createElement('div');
  bar.className = 'decision-bar';
  bar.innerHTML = `<span class="picked" id="picked-str">—</span>
    <button class="primary" id="send-decisions" disabled>선택 전송</button>
    <p class="hint">직접 입력하거나 조건을 덧붙이려면 아래 입력창을 쓰세요.</p>`;
  (last.querySelector('.body') || last).appendChild(bar);

  const refresh = () => {
    const order = cards.map((c) => c.dataset.decision);
    const parts = order.filter((id) => picked.has(id)).map((id) => picked.get(id));
    $('picked-str').textContent = parts.length ? parts.join(', ') : '—';
    $('send-decisions').disabled = parts.length !== cards.length;
  };

  last.querySelectorAll('.decision-opt').forEach((btn) => {
    btn.onclick = () => {
      const id = btn.dataset.decision;
      const card = btn.closest('.decision');
      card.querySelectorAll('.decision-opt').forEach((b) => b.classList.remove('selected'));
      btn.classList.add('selected');
      picked.set(id, btn.dataset.choice);
      refresh();
    };
  });

  $('send-decisions').onclick = () => {
    const order = cards.map((c) => c.dataset.decision);
    const answer = order.filter((id) => picked.has(id)).map((id) => picked.get(id)).join(', ');
    if (!answer) return;
    bar.remove();
    submitQuestion(answer);
  };

  refresh();
}

/* ── 복사 ────────────────────────────────────────────────
 * 답변은 원문 마크다운으로, 표는 TSV 로 준다 — 엑셀에 붙여넣으면 셀이 그대로 나뉘어야
 * 쓸모가 있고, HTML 을 복사하면 서식만 딸려오고 셀은 안 나뉜다. */
async function copyText(text, btn) {
  const prev = btn.textContent;
  try {
    await navigator.clipboard.writeText(text);
    btn.textContent = '✓ 복사됨';
  } catch (_) {
    // clipboard API 는 비보안 컨텍스트(http)나 권한 거부 시 실패한다 → 구식 경로로 대체
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    btn.textContent = document.execCommand('copy') ? '✓ 복사됨' : '복사 실패';
    ta.remove();
  }
  setTimeout(() => { btn.textContent = prev; }, 1400);
}

function tableToTsv(table) {
  return [...table.rows]
    .map((r) => [...r.cells].map((c) => c.innerText.replace(/\s+/g, ' ').trim()).join('\t'))
    .join('\n');
}

function wireCopy() {
  for (const b of document.querySelectorAll('[data-copy="answer"]')) {
    b.onclick = () => {
      const m = state.messages[Number(b.dataset.index)];
      if (m) copyText(m.content || '', b);
    };
  }
  // 표마다 복사 버튼을 하나씩 얹는다 — 보고서에 옮길 때 필요한 건 대개 표 하나다.
  for (const wrap of document.querySelectorAll('.md-table-wrap')) {
    if (wrap.dataset.copyWired) continue;
    wrap.dataset.copyWired = '1';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ghost sm table-copy';
    btn.textContent = '표 복사';
    btn.title = '엑셀에 붙여넣을 수 있는 형식(TSV)으로 복사';
    btn.onclick = () => copyText(tableToTsv(wrap.querySelector('table')), btn);
    wrap.prepend(btn);
  }
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
  const name = m ? decodeURIComponent(m[1])
    : (kind === 'html_report' ? 'report.html' : `${kind}.xlsx`);
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

// 안내문(placeholder)은 좁은 화면에서 두 줄로 감기고, rows=1 높이(44px)에 잘려 뒷부분이
// 안 보였다(실측 360px: 필요 68px vs 실제 44px). 자동 확장은 input 이벤트에만 걸려 있어
// 아직 입력이 없는 placeholder 는 그 혜택을 못 받는다 → 좁을 때는 짧은 문구를 쓴다.
const PLACEHOLDER_FULL = 'Instruct the associate…  (밸류에이션·데이터 질문)';
const PLACEHOLDER_SHORT = 'Instruct the associate…';

function fitPlaceholder() {
  questionBox.placeholder = window.matchMedia('(max-width: 640px)').matches
    ? PLACEHOLDER_SHORT : PLACEHOLDER_FULL;
}
fitPlaceholder();
window.matchMedia('(max-width: 640px)').addEventListener('change', fitPlaceholder);

// 입력창 최대 높이. 모바일 키보드가 올라오면 innerHeight 는 그대로인데 실제 보이는 영역만
// 줄어드는 기기가 있어(iOS) visualViewport 를 우선 쓴다.
function viewportHeight() {
  return (window.visualViewport && window.visualViewport.height) || window.innerHeight;
}

questionBox.addEventListener('input', () => {
  questionBox.style.height = 'auto';
  questionBox.style.height = `${Math.min(questionBox.scrollHeight, viewportHeight() * 0.4)}px`;
});

questionBox.addEventListener('keydown', (e) => {
  // Enter 전송 / Shift+Enter 줄바꿈 (기존 챗 UX 유지)
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    composer.requestSubmit();
  }
});

composer.addEventListener('submit', (e) => {
  e.preventDefault();
  const question = questionBox.value.trim();
  if (question) submitQuestion(question);
});

/* ── 사용 방법 가이드 ────────────────────────────────── */
function openHelp() { $('help-modal').hidden = false; }
function closeHelp() { $('help-modal').hidden = true; }

$('help-btn').addEventListener('click', openHelp);
$('help-close').addEventListener('click', closeHelp);
// 배경(모달 바깥)을 누르면 닫는다 — 상자 안쪽 클릭은 통과시키지 않는다.
$('help-modal').addEventListener('click', (e) => {
  if (e.target === $('help-modal')) closeHelp();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('help-modal').hidden) closeHelp();
});

// 예시 질문을 누르면 입력창에 넣고 닫는다. 바로 전송하지 않는 것은 의도적이다 —
// 회사명을 자기 것으로 바꿔 보내는 경우가 대부분이라 손댈 여지를 남긴다.
document.querySelectorAll('.ask-ex').forEach((b) => {
  b.addEventListener('click', () => {
    closeHelp();
    questionBox.value = b.dataset.ask || '';
    questionBox.focus();
    questionBox.dispatchEvent(new Event('input'));  // 자동 높이 조절을 태운다
  });
});

async function submitQuestion(question) {
  if (!question || state.busy) return;

  state.busy = true;
  stepAt = -1;              // 질문마다 단계를 처음부터
  $('send-btn').disabled = true;
  questionBox.value = '';
  questionBox.style.height = 'auto';
  dismissRetry();

  state.messages.push({ role: 'user', content: question });
  renderChat();

  // 서버가 이 질문을 **받았는지** 를 SSE 의 첫 이벤트(start)로 판정한다.
  // start 를 못 봤으면 서버 세션에 아무것도 기록되지 않은 상태다 → 입력을 되돌려야 한다.
  // 예전에는 전송 직후 입력창을 비우고 실패해도 복원하지 않아서, 네트워크가 끊기면
  // 사용자가 친 질문이 그냥 사라졌다(마감 앞두고 이걸 모르고 기다리면 최악이다).
  let serverAccepted = false;

  // 진행 중 표시 — 실제 tool 호출을 실시간으로 흘려보여준다.
  const live = document.createElement('div');
  live.className = 'msg assistant';
  live.innerHTML = `<div class="avatar">A</div><div class="body">
    <div class="steps" id="live-steps">${STEPS.map((s, n) =>
      `<span class="step" data-step="${n}"><i>${n + 1}</i>${esc(s.label)}</span>`).join('')}</div>
    <details class="trace" open><summary><span class="spinner"></span> 데이터 소스 조회 중…
      <button type="button" class="ghost sm cancel-btn" id="cancel-btn">중단</button></summary>
    <div class="trace-body" id="live-trace"></div></details></div>`;
  $('chat').appendChild(live);
  scrollToEnd();
  const liveBody = $('live-trace');
  markStep(0);

  const append = (html) => {
    liveBody.insertAdjacentHTML('beforeend', html);
    scrollToEnd();
  };

  // 긴 조사를 사용자가 끊을 수 있어야 한다. AbortController 로 스트림을 끊으면
  // 서버는 이미 받은 질문을 계속 처리하지만, 화면은 즉시 돌려준다.
  const ctrl = new AbortController();
  state.abort = ctrl;
  let cancelled = false;
  $('cancel-btn').onclick = () => { cancelled = true; ctrl.abort(); };

  try {
    const res = await fetch('/api/ask', {
      method: 'POST',
      signal: ctrl.signal,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question, session_id: state.sessionId,
        provider: state.provider, model: state.model,
        reasoning: state.reasoning,
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
          serverAccepted = true;      // 여기부터는 서버 세션에 기록된다
          state.sessionId = ev.session_id;
        } else if (ev.type === 'tool_use') {
          markStep(stepOf(ev.name));
          append(`<div class="trace-live">🔧 호출: ${esc(ev.name)}(${esc(fmtArgs(ev.input))})</div>`);
        } else if (ev.type === 'progress') {
          append(`<div class="trace-live">🔧 ${esc(ev.text)}</div>`);
        } else if (ev.type === 'tool_result') {
          append(traceItemHtml(ev));
        } else if (ev.type === 'error') {
          append(`<div class="trace-item err">${esc(ev.text)}</div>`);
        } else if (ev.type === 'final') {
          markStep(STEPS.length - 1);
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
    if (cancelled) {
      // 사용자가 끊은 것은 오류가 아니다. 다만 서버가 이미 질문을 받았다면 그 턴은
      // 서버 세션에 저장되므로, 화면에도 '중단됨' 을 남겨 기록과 어긋나지 않게 한다.
      state.messages.push({
        role: 'assistant', content: '⏹ 사용자가 중단했습니다.',
        html: '<p>⏹ 사용자가 중단했습니다.</p>', trace: [],
      });
    } else if (serverAccepted) {
      // 서버는 받았고 처리 중에 끊긴 경우 — 질문은 세션에 남아 있으니 되돌리지 않는다.
      // 여기서 입력창에 원문을 복원하면 사용자가 같은 질문을 두 번 보내게 된다.
      state.messages.push({
        role: 'assistant',
        content: `⚠️ ${err.message}`,
        html: `<p>⚠️ ${esc(err.message)}</p>`,
        trace: [],
      });
    } else {
      // 서버에 도달하지 못했다 — 방금 낙관적으로 그린 사용자 메시지를 걷어내고,
      // 친 내용을 입력창에 돌려준 뒤 재시도 배너를 띄운다.
      const last = state.messages[state.messages.length - 1];
      if (last && last.role === 'user' && last.content === question) state.messages.pop();
      restoreQuestion(question);
      showRetry(question, err.message);
    }
  } finally {
    live.remove();
    state.busy = false;
    renderChat();
    renderSessions();
    renderEngine();
    questionBox.focus();
  }
}

/* ── 전송 실패 복구 ──────────────────────────────────── */
function restoreQuestion(question) {
  // 사용자가 그 사이 다른 질문을 치고 있었다면 덮어쓰지 않는다.
  if (questionBox.value.trim()) return;
  questionBox.value = question;
  questionBox.style.height = 'auto';
  questionBox.style.height = `${Math.min(questionBox.scrollHeight, viewportHeight() * 0.4)}px`;
}

function dismissRetry() {
  const el = $('retry-banner');
  if (el) el.hidden = true;
}

function showRetry(question, reason) {
  const el = $('retry-banner');
  if (!el) return;
  el.hidden = false;
  el.querySelector('.retry-msg').textContent =
    `전송하지 못했습니다 (${reason}). 질문은 입력창에 그대로 있습니다.`;
  const btn = el.querySelector('.retry-btn');
  btn.onclick = () => {
    dismissRetry();
    const text = questionBox.value.trim() || question;
    submitQuestion(text);
  };
}

/* ── 사이드바 (좁은 화면에서는 오버레이) ─────────────── */
function setSidebar(open) {
  $('sidebar').classList.toggle('collapsed', !open);
  $('sidebar-backdrop').hidden = !open;
  // 사이드바가 열린 동안 뒤 본문이 스크롤되면 방향감각을 잃는다.
  document.body.style.overflow = open ? 'hidden' : '';
}

function sidebarIsOverlay() {
  return window.matchMedia('(max-width: 860px)').matches;
}

$('sidebar-toggle').addEventListener('click', () => {
  setSidebar($('sidebar').classList.contains('collapsed'));
});
$('sidebar-backdrop').addEventListener('click', () => setSidebar(false));

// 오버레이 상태에서 대화를 고르거나 새로 만들면 사이드바가 화면을 덮은 채 남아
// 결과가 안 보인다 → 선택 즉시 닫는다.
$('session-list').addEventListener('click', () => {
  if (sidebarIsOverlay()) setSidebar(false);
});
$('new-session').addEventListener('click', () => {
  if (sidebarIsOverlay()) setSidebar(false);
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && sidebarIsOverlay()) setSidebar(false);
});

// 데스크톱 폭으로 돌아오면 사이드바는 항상 보이는 상태여야 한다(백드롭도 치운다).
window.matchMedia('(max-width: 860px)').addEventListener('change', (e) => {
  if (!e.matches) {
    $('sidebar').classList.remove('collapsed');
    $('sidebar-backdrop').hidden = true;
    document.body.style.overflow = '';
  } else {
    setSidebar(false);
  }
});

// 폰에서는 처음에 사이드바를 접어 본문부터 보여준다.
if (sidebarIsOverlay()) setSidebar(false);

/* ── 모바일 키보드 ───────────────────────────────────── */
// iOS 는 키보드가 올라올 때 뷰포트를 밀어올리기만 해서 입력창이 키보드 뒤로 숨는다.
// visualViewport 로 실제 보이는 높이를 CSS 변수에 넣어 shell 높이를 맞춘다.
if (window.visualViewport) {
  const fit = () => {
    const vv = window.visualViewport;
    document.documentElement.style.setProperty('--vvh', `${vv.height}px`);
    // 키보드가 올라온 동안에는 마지막 메시지가 보이도록 붙여둔다.
    if (document.activeElement === $('question')) scrollToEnd();
  };
  window.visualViewport.addEventListener('resize', fit);
  window.visualViewport.addEventListener('scroll', fit);
  fit();
}

/* ── PWA: 서비스워커 + 홈 화면 설치 ──────────────────── */
// SW 는 https 또는 localhost 에서만 등록된다(파일로 열면 조용히 실패하는 게 정상).
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' })
      .then((reg) => {
        // 새 버전이 준비되면 다음 진입에서 바로 쓰이게 교체를 요청한다.
        reg.addEventListener('updatefound', () => {
          const sw = reg.installing;
          if (sw) sw.addEventListener('statechange', () => {
            if (sw.state === 'installed' && navigator.serviceWorker.controller) {
              sw.postMessage('skip-waiting');
            }
          });
        });
      })
      .catch(() => { /* 등록 실패는 앱 동작에 영향 없음 — 조용히 넘긴다 */ });

    // 새 서비스워커가 제어를 넘겨받으면 한 번만 새로고침한다.
    // 이게 없으면 배포 직후 첫 접속이 '새 HTML + 옛 JS' 로 뜬다 — 이번 페이지의 자산은
    // 이미 옛 워커가 응답한 뒤이기 때문이다(실측: '사용 방법' 버튼이 눌리지 않던 원인).
    // reloaded 플래그로 무한 새로고침을 막는다.
    let reloaded = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (reloaded) return;
      reloaded = true;
      location.reload();
    });
  });
}

// 안드로이드/데스크톱 Chrome 은 설치 프롬프트를 코드로 띄울 수 있다. iOS 는 불가능해서
// (Safari 공유 → '홈 화면에 추가' 만 가능) 그 경우엔 안내 문구로 대체한다.
let installPrompt = null;
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  installPrompt = e;
  const btn = $('install-btn');
  if (btn) btn.hidden = false;
});

function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true;
}

function initInstallUi() {
  const btn = $('install-btn');
  const hint = $('install-hint');
  if (!btn || !hint) return;
  if (isStandalone()) return;            // 이미 앱으로 실행 중

  const isIos = /iP(hone|ad|od)/.test(navigator.userAgent);
  if (isIos) {
    hint.hidden = false;
    hint.textContent = '홈 화면에 추가: 공유 버튼 → "홈 화면에 추가"';
    return;
  }
  btn.addEventListener('click', async () => {
    if (!installPrompt) return;
    installPrompt.prompt();
    const { outcome } = await installPrompt.userChoice;
    installPrompt = null;
    if (outcome === 'accepted') btn.hidden = true;
  });
}

window.addEventListener('appinstalled', () => {
  const btn = $('install-btn');
  if (btn) btn.hidden = true;
});

/* ── 시작 ───────────────────────────────────────────── */
setTimeout(() => { const s = $('splash'); if (s) s.remove(); }, 2300);
initInstallUi();
boot();
