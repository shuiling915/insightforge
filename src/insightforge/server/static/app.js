const API = '';
let token = localStorage.getItem('if_token');
let currentUser = null;
let currentSession = null;
let currentRunId = null;
let isRunning = false;
let eventSource = null;

const typeLabels = {
  thinking: '思考',
  plan_created: '规划',
  plan_updated: '规划',
  plan_step_completed: '步骤完成',
  code_executing: '执行代码',
  code_success: '代码执行成功',
  code_failed: '代码执行失败',
  output_validation_failed: '输出校验失败',
  output_validation_retry: '输出校验重试',
  answer_started: '生成答案',
  answer_delta: '答案',
  answer_finished: '最终答案',
  subagent_started: '子 Agent 启动',
  subagent_finished: '子 Agent 完成',
  agent_started: '开始',
  agent_finished: '完成',
  agent_error: '错误',
  agent_cancelled: '已取消',
  round_started: '轮次开始',
  round_finished: '轮次结束',
  llm_call_started: '调用模型',
  llm_call_finished: '模型响应',
  llm_fallback: '模型回退',
  context_pruned: '上下文裁剪',
  tool_result: '工具结果',
  system: '系统'
};

let currentAnswerEl = null;

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function renderMarkdown(text) {
  if (!text) return '';
  let html = escapeHtml(text);

  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (m, lang, code) => {
    return `<div class="code-block"><div class="code-toolbar"><button class="copy-btn" onclick="copyCode(this)">复制</button></div><pre><code>${code}</code></pre></div>`;
  });

  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  html = html.split('\n').map(line => {
    if (line.startsWith('### ')) return `<h3>${line.slice(4)}</h3>`;
    if (line.startsWith('## ')) return `<h2>${line.slice(3)}</h2>`;
    if (line.startsWith('# ')) return `<h1>${line.slice(2)}</h1>`;
    if (line.startsWith('---')) return '<hr>';
    if (line.startsWith('> ')) return `<blockquote>${line.slice(2)}</blockquote>`;
    if (/^\d+\.\s/.test(line)) return `<li>${line.replace(/^\d+\.\s/, '')}</li>`;
    if (line.startsWith('- ')) return `<li>${line.slice(2)}</li>`;
    return line;
  }).join('\n');

  html = html.replace(/(<li>.*<\/li>\n?)+/g, m => `<ul>${m}</ul>`);

  html = html.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*([^*]+)\*/g, '<em>$1</em>');
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank">$1</a>');

  html = html.replace(/\n{2,}/g, '</p><p>');
  html = `<p>${html}</p>`;
  html = html.replace(/<p>\s*<(h\d|ul|ol|blockquote|hr|div)/g, '<$1');
  html = html.replace(/<\/(h\d|ul|ol|blockquote)>\s*<\/p>/g, '</$1>');

  return html;
}

function copyCode(btn) {
  const code = btn.closest('.code-block').querySelector('code').textContent;
  navigator.clipboard.writeText(code).then(() => {
    btn.textContent = '已复制';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = '复制'; btn.classList.remove('copied'); }, 1500);
  });
}

function showToast(msg, type = 'info') {
  const toast = document.getElementById('toast');
  toast.textContent = msg;
  toast.className = `toast ${type} show`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { toast.className = 'toast'; }, 2500);
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
}

function fmtDate(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

function authHeaders() {
  const h = { 'Content-Type': 'application/json' };
  if (token) h['Authorization'] = `Bearer ${token}`;
  return h;
}

async function api(url, opts = {}) {
  const res = await fetch(API + url, { ...opts, headers: { ...authHeaders(), ...(opts.headers || {}) } });
  if (res.status === 401) {
    logout(false);
    throw new Error('未登录');
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

async function checkAuth() {
  if (!token) return showLogin();
  try {
    const data = await api('/auth/me');
    if (!data.authenticated) {
      logout(false);
      return;
    }
    currentUser = data.user;
    if (currentUser.must_change_password) {
      showApp();
      openChangePassword(true);
    } else {
      showApp();
    }
  } catch {
    logout(false);
  }
}

function showLogin() {
  document.getElementById('loginOverlay').style.display = 'flex';
}

function showApp() {
  document.getElementById('loginOverlay').style.display = 'none';
  if (currentUser) {
    document.getElementById('userBadge').innerHTML =
      `${currentUser.username} <span class="role-badge role-${currentUser.role}">${currentUser.role}</span>`;
    document.getElementById('userInfo').style.display = 'flex';
    document.getElementById('adminBtn').style.display = currentUser.role === 'admin' ? 'inline-block' : 'none';
  }
  loadSessions();
}

async function doLogin() {
  const u = document.getElementById('loginUser').value.trim();
  const p = document.getElementById('loginPass').value;
  if (!u || !p) return;
  try {
    const data = await api('/auth/login', { method: 'POST', body: JSON.stringify({ username: u, password: p }) });
    token = data.token;
    currentUser = data.user;
    localStorage.setItem('if_token', token);
    document.getElementById('loginError').textContent = '';
    showApp();
  } catch (e) {
    document.getElementById('loginError').textContent = e.message;
  }
}

function logout(showMsg = true) {
  if (token) {
    api('/auth/logout', { method: 'POST' }).catch(() => {});
  }
  token = null;
  currentUser = null;
  localStorage.removeItem('if_token');
  document.getElementById('userInfo').style.display = 'none';
  showLogin();
  if (showMsg) showToast('已退出登录', 'info');
}

function openChangePassword(forced = false) {
  document.getElementById('chgPwOverlay').style.display = 'flex';
  document.getElementById('chgPwForced').style.display = forced ? 'block' : 'none';
  document.getElementById('chgPwCancel').style.display = forced ? 'none' : 'block';
  document.getElementById('chgPwOld').value = '';
  document.getElementById('chgPwNew').value = '';
  document.getElementById('chgPwConfirm').value = '';
  document.getElementById('chgPwError').textContent = '';
}

function closeChangePassword() {
  document.getElementById('chgPwOverlay').style.display = 'none';
}

async function doChangePassword() {
  const oldPw = document.getElementById('chgPwOld').value;
  const newPw = document.getElementById('chgPwNew').value;
  const confirmPw = document.getElementById('chgPwConfirm').value;
  const err = document.getElementById('chgPwError');

  if (!oldPw || !newPw || !confirmPw) { err.textContent = '请填写所有字段'; return; }
  if (newPw !== confirmPw) { err.textContent = '两次输入的新密码不一致'; return; }
  if (newPw.length < 8) { err.textContent = '密码至少 8 位'; return; }
  if (!/[A-Z]/.test(newPw) || !/[a-z]/.test(newPw) || !/\d/.test(newPw)) {
    err.textContent = '密码需包含大小写字母和数字'; return;
  }

  try {
    await api('/auth/change-password', { method: 'POST', body: JSON.stringify({ old_password: oldPw, new_password: newPw }) });
    closeChangePassword();
    showToast('密码修改成功', 'success');
    currentUser.must_change_password = false;
  } catch (e) {
    err.textContent = e.message;
  }
}

async function loadSessions() {
  try {
    const sessions = await api('/sessions');
    const list = document.getElementById('sessionList');
    list.innerHTML = '';
    if (!sessions.length) {
      list.innerHTML = '<div style="padding:16px;color:var(--text-muted);font-size:12px;text-align:center;">暂无会话</div>';
      return;
    }
    sessions.forEach(s => {
      const item = document.createElement('div');
      item.className = 'session-item' + (s.id === currentSession ? ' active' : '');
      item.innerHTML = `
        <div class="title">${escapeHtml(s.title || '新会话')}</div>
        <div class="meta">${fmtDate(s.created_at)} · ${s.event_count || 0} 条事件</div>
        <button class="delete-btn" onclick="deleteSession(event, '${s.id}')" title="删除">×</button>
      `;
      item.onclick = (e) => { if (e.target.classList.contains('delete-btn')) return; loadSession(s.id); };
      list.appendChild(item);
    });
  } catch (e) {
    console.error('加载会话失败', e);
  }
}

function createSession() {
  currentSession = null;
  document.querySelectorAll('.session-item').forEach(i => i.classList.remove('active'));
  document.getElementById('events').innerHTML = `
    <div class="empty-state">
      <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <p>开始新的数据分析</p>
      <p class="hint">输入你的问题，AI 将自动查询数据库并给出分析结果</p>
    </div>`;
}

async function loadSession(sessionId) {
  currentSession = sessionId;
  loadSessions();
  try {
    const data = await api(`/sessions/${sessionId}`);
    const events = document.getElementById('events');
    events.innerHTML = '';
    if (data.events && data.events.length) {
      data.events.forEach(e => appendEvent(e));
    } else {
      events.innerHTML = '<div class="empty-state"><p>该会话暂无内容</p></div>';
    }
  } catch (e) {
    console.error(e);
  }
}

async function deleteSession(e, sessionId) {
  e.stopPropagation();
  if (!confirm('确定删除该会话？')) return;
  try {
    await api(`/sessions/${sessionId}`, { method: 'DELETE' });
    if (currentSession === sessionId) createSession();
    loadSessions();
    showToast('会话已删除', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

function extractContent(evt) {
  if (evt.delta) return evt.delta;
  if (evt.message) return evt.message;
  if (evt.code) return evt.code;
  if (evt.error) return evt.error;
  if (evt.result) {
    const r = evt.result;
    if (r.output) return r.output;
    if (r.stdout) return r.stdout + (r.stderr ? '\n' + r.stderr : '');
    if (r.error) return r.error;
    return '(No output)';
  }
  if (evt.data && typeof evt.data === 'object') {
    return JSON.stringify(evt.data, null, 2);
  }
  return '';
}

function appendEvent(evt) {
  const events = document.getElementById('events');
  const empty = events.querySelector('.empty-state');
  if (empty) empty.remove();

  const type = evt.type || 'system';
  const label = typeLabels[type] || type;

  if (type === 'answer_delta') {
    if (!currentAnswerEl) {
      currentAnswerEl = document.createElement('div');
      currentAnswerEl.className = 'event answer';
      currentAnswerEl.innerHTML = `<div class="event-header"><span>最终答案</span></div><div class="event-body markdown"></div>`;
      events.appendChild(currentAnswerEl);
    }
    const body = currentAnswerEl.querySelector('.event-body');
    body.textContent += evt.delta || '';
    events.scrollTop = events.scrollHeight;
    return;
  }

  if (type === 'answer_started') {
    currentAnswerEl = null;
    return;
  }

  currentAnswerEl = null;
  const div = document.createElement('div');
  div.className = `event ${type}`;

  let body = '';
  const content = extractContent(evt);

  if (type === 'thinking') {
    body = `<div class="event-body">${escapeHtml(content)}</div>`;
  } else if (type === 'plan_updated' || type === 'plan_created') {
    if (evt.plan && evt.plan.steps) {
      const steps = evt.plan.steps.map(s =>
        `<li class="${s.completed ? 'done' : ''}"><span class="check-icon">${s.completed ? '✓' : '○'}</span>${escapeHtml(s.description || '')}</li>`
      ).join('');
      body = `<div class="event-body"><ul class="plan-checklist">${steps}</ul></div>`;
    } else if (content) {
      body = `<div class="event-body">${escapeHtml(content)}</div>`;
    }
  } else if (type === 'code_executing') {
    body = `<div class="event-body"><div class="code-block"><div class="code-toolbar"><button class="copy-btn" onclick="copyCode(this)">复制</button></div><pre><code>${escapeHtml(evt.code || content)}</code></pre></div></div>`;
  } else if (type === 'code_success') {
    const out = evt.result ? (evt.result.output || evt.result.stdout || '') : content;
    body = `<div class="event-body"><div class="output-block"><pre>${escapeHtml(out)}</pre></div></div>`;
  } else if (type === 'code_failed' || type === 'agent_error') {
    const err = evt.error || (evt.result && evt.result.error) || content;
    body = `<div class="event-body"><div class="error-block"><pre>${escapeHtml(err)}</pre></div></div>`;
  } else if (type === 'answer_finished') {
    body = `<div class="event-body markdown">${renderMarkdown(content)}</div>`;
  } else if (type === 'agent_started' || type === 'agent_finished' || type === 'agent_cancelled' ||
             type === 'round_started' || type === 'round_finished' || type === 'llm_call_started' ||
             type === 'llm_call_finished' || type === 'llm_fallback' || type === 'context_pruned' ||
             type === 'plan_step_completed' || type === 'subagent_started' || type === 'subagent_finished' ||
             type === 'output_validation_failed' || type === 'output_validation_retry') {
    if (content) {
      body = `<div class="event-body" style="color:var(--text-secondary);font-size:13px;">${escapeHtml(content)}</div>`;
    }
  } else {
    body = content ? `<div class="event-body">${escapeHtml(content)}</div>` : '';
  }

  const tokens = evt.tokens_used ? `<span class="token-info">${evt.tokens_used} tokens</span>` : '';
  div.innerHTML = `<div class="event-header"><span>${label}</span><span>${tokens}</span></div>${body}`;
  events.appendChild(div);
  events.scrollTop = events.scrollHeight;
}

async function sendMessage() {
  const input = document.getElementById('taskInput');
  const task = input.value.trim();
  if (!task || isRunning) return;
  if (currentUser && currentUser.role === 'viewer') {
    showToast('viewer 角色没有分析权限', 'error');
    return;
  }

  isRunning = true;
  input.value = '';
  input.disabled = true;
  document.getElementById('sendBtn').disabled = true;
  document.getElementById('cancelBtn').style.display = 'inline-block';

  const events = document.getElementById('events');
  const empty = events.querySelector('.empty-state');
  if (empty) empty.remove();

  try {
    const res = await api('/runs', {
      method: 'POST',
      body: JSON.stringify({ task, session_id: currentSession })
    });
    currentRunId = res.run_id;
    if (!currentSession) {
      currentSession = res.session_id;
      loadSessions();
    }
    startStream(currentRunId, task, currentSession);
  } catch (e) {
    showToast(e.message, 'error');
    resetInput();
  }
}

function startStream(runId, task, sessionId) {
  const params = new URLSearchParams({ task });
  if (sessionId) params.set('session_id', sessionId);
  if (token) params.set('token', token);
  const url = `${API}/runs/${runId}/stream?${params.toString()}`;
  eventSource = new EventSource(url);

  const handleSSE = (e) => {
    try {
      const evt = JSON.parse(e.data);
      appendEvent(evt);
    } catch (err) {
      console.error('SSE parse error', err);
    }
  };

  const terminalTypes = ['agent_finished', 'agent_error', 'agent_cancelled'];
  terminalTypes.forEach(type => {
    eventSource.addEventListener(type, (e) => {
      handleSSE(e);
      eventSource.close();
      resetInput();
      loadSessions();
    });
  });

  Object.keys(typeLabels).forEach(type => {
    if (!terminalTypes.includes(type)) {
      eventSource.addEventListener(type, handleSSE);
    }
  });
  eventSource.addEventListener('done', () => {
    eventSource.close();
    resetInput();
    loadSessions();
  });
  eventSource.addEventListener('error', (e) => {
    if (e.data) {
      try { appendEvent(JSON.parse(e.data)); } catch {}
    }
    if (eventSource.readyState === EventSource.CLOSED) {
      resetInput();
    }
  });
  eventSource.onmessage = handleSSE;
}

function resetInput() {
  isRunning = false;
  currentRunId = null;
  eventSource = null;
  const input = document.getElementById('taskInput');
  input.disabled = false;
  document.getElementById('sendBtn').disabled = false;
  document.getElementById('cancelBtn').style.display = 'none';
  input.focus();
}

async function cancelRun() {
  if (!currentRunId) return;
  try {
    await api(`/runs/${currentRunId}/cancel`, { method: 'POST' });
    showToast('正在取消...', 'info');
  } catch (e) {
    showToast(e.message, 'error');
  }
}

function autoResize(textarea) {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 200) + 'px';
}

function useExample(text) {
  document.getElementById('taskInput').value = text;
  autoResize(document.getElementById('taskInput'));
  document.getElementById('taskInput').focus();
}

async function openAdmin() {
  if (!currentUser || currentUser.role !== 'admin') {
    showToast('仅管理员可访问', 'error');
    return;
  }
  document.getElementById('adminOverlay').style.display = 'flex';
  await loadUsers();
}

function closeAdmin() {
  document.getElementById('adminOverlay').style.display = 'none';
}

async function loadUsers() {
  try {
    const users = await api('/admin/users');
    const tbody = document.getElementById('usersTable').querySelector('tbody');
    tbody.innerHTML = '';
    users.forEach(u => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${escapeHtml(u.username)}</td>
        <td><span class="role-badge role-${u.role}">${u.role}</span></td>
        <td>
          <select onchange="changeRole(${u.id}, this.value)">
            <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>admin</option>
            <option value="analyst" ${u.role === 'analyst' ? 'selected' : ''}>analyst</option>
            <option value="viewer" ${u.role === 'viewer' ? 'selected' : ''}>viewer</option>
          </select>
        </td>
        <td>${u.is_active ? '✅' : '🚫'}</td>
        <td>
          <button class="btn-icon" onclick="toggleActive(${u.id}, ${!u.is_active})">${u.is_active ? '禁用' : '启用'}</button>
          <button class="btn-icon" onclick="resetPwd(${u.id})">重置密码</button>
          <button class="btn-icon" style="color:var(--danger)" onclick="deleteUser(${u.id})">删除</button>
        </td>
      `;
      tbody.appendChild(tr);
    });
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function openRegister() {
  document.getElementById('regOverlay').style.display = 'flex';
  document.getElementById('regError').textContent = '';
}

function closeRegister() {
  document.getElementById('regOverlay').style.display = 'none';
}

async function doRegister() {
  const u = document.getElementById('regUser').value.trim();
  const p = document.getElementById('regPass').value;
  const r = document.getElementById('regRole').value;
  const err = document.getElementById('regError');
  if (!u || !p) { err.textContent = '请填写用户名和密码'; return; }
  try {
    await api('/auth/register', { method: 'POST', body: JSON.stringify({ username: u, password: p, role: r }) });
    closeRegister();
    loadUsers();
    showToast('用户创建成功', 'success');
  } catch (e) {
    err.textContent = e.message;
  }
}

async function changeRole(id, role) {
  try {
    await api(`/admin/users/${id}/role`, { method: 'PUT', body: JSON.stringify({ role }) });
    showToast('角色已更新', 'success');
  } catch (e) {
    showToast(e.message, 'error');
    loadUsers();
  }
}

async function toggleActive(id, active) {
  try {
    await api(`/admin/users/${id}/active`, { method: 'PUT', body: JSON.stringify({ active }) });
    loadUsers();
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function resetPwd(id) {
  const np = prompt('请输入新密码（至少8位，含大小写字母和数字）：');
  if (!np) return;
  try {
    await api(`/admin/users/${id}/password`, { method: 'PUT', body: JSON.stringify({ new_password: np }) });
    showToast('密码已重置', 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
}

async function deleteUser(id) {
  if (!confirm('确定删除该用户？')) return;
  try {
    await api(`/admin/users/${id}`, { method: 'DELETE' });
    loadUsers();
    showToast('用户已删除', 'success');
  } catch (e) {
    showToast(e.message, 'error');
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('loginPass').addEventListener('keypress', e => { if (e.key === 'Enter') doLogin(); });
  document.getElementById('taskInput').addEventListener('input', e => autoResize(e.target));
  document.getElementById('taskInput').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  document.getElementById('chgPwConfirm').addEventListener('keypress', e => { if (e.key === 'Enter') doChangePassword(); });
  checkAuth();
});