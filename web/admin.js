/* 관리자 페이지 — 계정별 사용현황 + 대화 열람.
 * 데이터는 전부 /api/admin/* 에서 오고, 권한 검사도 서버가 한다(이 파일은 화면만 그린다).
 */
'use strict';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmtTime = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return esc(iso);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
       + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

const ago = (iso) => {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (Number.isNaN(diff)) return '';
  if (diff < 3600) return `${Math.max(1, Math.floor(diff / 60))}분 전`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}시간 전`;
  return `${Math.floor(diff / 86400)}일 전`;
};

async function api(path) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' } });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* 본문 없음 */ }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function renderTotals(t) {
  const tiles = [
    ['사람', t.members], ['계정', t.users], ['대화', t.sessions], ['질문', t.questions],
    ['도구 호출', t.tool_calls], ['실패 호출', t.failed_calls],
  ];
  $('totals').innerHTML = tiles.map(([label, v]) => `
    <div class="tile"><div class="tile-v">${v}</div><div class="tile-l">${label}</div></div>`).join('');
}

/* 사람 단위 — 한 계정을 여러 명이 공유해도 누가 얼마나 썼는지 갈라 보인다. */
function renderMembers(members) {
  if (!members.length) {
    $('members-table').innerHTML =
      '<tbody><tr><td class="hint">아직 기록이 없습니다.</td></tr></tbody>';
    return;
  }
  $('members-table').innerHTML = `
    <thead><tr><th>이름</th><th>계정</th><th class="num">질문</th><th>마지막 활동</th></tr></thead>
    <tbody>${members.map((m) => `
      <tr>
        <td><strong>${esc(m.name)}</strong>${m.name === m.account
            ? ' <span class="chip">이름 미기입</span>' : ''}</td>
        <td>${esc(m.account)}</td>
        <td class="num">${m.questions}</td>
        <td>${fmtTime(m.last_active)}<span class="hint"> ${ago(m.last_active)}</span></td>
      </tr>`).join('')}</tbody>`;
}

function renderUsers(users) {
  $('users-table').innerHTML = `
    <thead><tr><th>계정</th><th>사람</th><th class="num">대화</th><th class="num">질문</th>
      <th class="num">도구 호출</th><th class="num">실패</th><th>마지막 활동</th><th>최근 대화</th></tr></thead>
    <tbody>${users.map((u) => `
      <tr>
        <td><strong>${esc(u.name)}</strong>${u.is_admin ? ' <span class="chip">admin</span>' : ''}</td>
        <td>${(u.members || []).length
            ? (u.members || []).map((m) => esc(m)).join(', ') : '<span class="hint">—</span>'}</td>
        <td class="num">${u.sessions}</td>
        <td class="num">${u.questions}</td>
        <td class="num">${u.tool_calls}</td>
        <td class="num${u.failed_calls ? ' bad' : ''}">${u.failed_calls}</td>
        <td>${fmtTime(u.last_active)}<span class="hint"> ${ago(u.last_active)}</span></td>
        <td>${u.recent_sessions.length ? u.recent_sessions.map((s) => `
              <button class="linky" data-user="${esc(u.name)}" data-sid="${esc(s.id)}"
                      title="${esc(s.first_question || '')}">${esc(s.title)}</button>`).join('')
            : '<span class="hint">아직 없음</span>'}</td>
      </tr>`).join('')}</tbody>`;
}

function renderTimeline(rows) {
  if (!rows.length) {
    $('timeline-table').innerHTML = '<tbody><tr><td class="hint">아직 질문이 없습니다.</td></tr></tbody>';
    return;
  }
  $('timeline-table').innerHTML = `
    <thead><tr><th>시각</th><th>사람</th><th>계정</th><th>질문</th></tr></thead>
    <tbody>${rows.map((r) => `
      <tr>
        <td class="nowrap">${fmtTime(r.at)}</td>
        <td class="nowrap"><strong>${esc(r.member || r.user)}</strong></td>
        <td>${esc(r.user)}</td>
        <td><button class="linky wide" data-user="${esc(r.user)}" data-sid="${esc(r.session_id)}"
            >${esc((r.question || '').slice(0, 140))}</button></td>
      </tr>`).join('')}</tbody>`;
}

function renderTools(tools) {
  if (!tools.length) {
    $('tools-table').innerHTML = '<tbody><tr><td class="hint">아직 사용 기록이 없습니다.</td></tr></tbody>';
    return;
  }
  const max = tools[0].count || 1;
  $('tools-table').innerHTML = `
    <thead><tr><th>도구</th><th class="num">호출</th><th>비중</th></tr></thead>
    <tbody>${tools.map((t) => `
      <tr>
        <td><code>${esc(t.name)}</code></td>
        <td class="num">${t.count}</td>
        <td><span class="bar" style="width:${Math.round((t.count / max) * 100)}%"></span></td>
      </tr>`).join('')}</tbody>`;
}

function wireSessionLinks() {
  document.querySelectorAll('[data-sid]').forEach((b) => {
    b.onclick = () => openSession(b.dataset.user, b.dataset.sid);
  });
}

async function openSession(user, sid) {
  $('modal').hidden = false;
  $('modal-title').textContent = `${user} · 대화`;
  $('modal-meta').textContent = '불러오는 중…';
  $('modal-body').innerHTML = '';
  try {
    const rec = await api(`/api/admin/sessions/${encodeURIComponent(user)}/${encodeURIComponent(sid)}`);
    $('modal-title').textContent = `${user} · ${rec.title || '대화'}`;
    $('modal-meta').textContent = `${fmtTime(rec.created_at)} 시작 · ${fmtTime(rec.updated_at)} 갱신`;
    $('modal-body').innerHTML = rec.messages.map((m) => {
      if (m.role === 'user') {
        return `<div class="msg user"><div class="avatar">Q</div>
                  <div class="body">${m.by ? `<div class="hint">${esc(m.by)}</div>` : ''}
                  ${esc(m.content)}</div></div>`;
      }
      const trace = m.trace || [];
      const items = trace.map((t) => {
        const r = t.result || {};
        if (!r.ok) {
          return `<div class="trace-item err"><code>${esc(t.name)}</code>
                    <div class="meta">${esc(String(r.error || '실패').slice(0, 200))}</div></div>`;
        }
        const v = r.value || {};
        const p = v.provenance || {};
        return `<div class="trace-item"><div class="head">
                  <code>${esc(t.name)}</code>
                  <span class="val">→ ${esc(v.value)} ${esc(v.unit || '')}</span></div>
                  <div class="meta">출처 ${esc(p.source || '')}${p.as_of ? ` · ${esc(p.as_of)}` : ''}</div>
                </div>`;
      }).join('');
      return `<div class="msg assistant"><div class="avatar">A</div><div class="body">
                ${trace.length ? `<details class="trace"><summary>🔍 사용한 데이터 소스 (${trace.length}개)</summary>
                  <div class="trace-body">${items}</div></details>` : ''}
                <div class="md">${m.html || esc(m.content)}</div></div></div>`;
    }).join('');
  } catch (e) {
    $('modal-meta').textContent = '';
    $('modal-body').innerHTML = `<p class="warn box">${esc(e.message)}</p>`;
  }
}

$('modal-close').onclick = () => { $('modal').hidden = true; };
$('modal').onclick = (e) => { if (e.target === $('modal')) $('modal').hidden = true; };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') $('modal').hidden = true;
});

async function load() {
  const err = $('admin-error');
  err.hidden = true;
  try {
    const d = await api('/api/admin/overview');
    $('admin-body').hidden = false;
    $('admin-sub').textContent =
      `${d.viewer.label} 로 로그인 · 사람 ${d.totals.members}명 / 계정 ${d.totals.users}개 `
      + `· 질문 ${d.totals.questions}건`;
    $('admin-names').textContent = (d.admins || []).join(', ');
    renderTotals(d.totals);
    renderMembers(d.members || []);
    renderUsers(d.users);
    renderTimeline(d.timeline);
    renderTools(d.tools);
    wireSessionLinks();
  } catch (e) {
    $('admin-body').hidden = true;
    err.hidden = false;
    err.textContent = e.status === 403
      ? '관리자 계정으로 로그인해야 볼 수 있습니다. (ADMIN_USERS 환경변수로 지정)'
      : (e.status === 401 ? '로그인이 필요합니다. 앱에서 로그인한 뒤 다시 열어주세요.' : e.message);
  }
}

$('refresh-btn').onclick = load;
load();
