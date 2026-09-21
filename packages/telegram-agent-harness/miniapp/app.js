const app = window.Telegram?.WebApp;
app?.ready(); app?.expand();
const byId = id => document.getElementById(id);
const empty = (id, message) => { byId(id).replaceChildren(); const p = document.createElement('p'); p.className = 'empty'; p.textContent = message; byId(id).append(p); };
function card(id, title, detail, state) {
  const node = document.createElement('article');
  const heading = document.createElement('h3'); heading.textContent = title;
  const p = document.createElement('p'); p.textContent = detail;
  node.append(heading, p);
  if (state) { const badge = document.createElement('small'); badge.textContent = state; node.append(badge); }
  byId(id).append(node); return node;
}
function unavailable() {
  for (const id of ['sessions', 'approvals', 'lanes', 'blockers', 'queue']) empty(id, 'Unavailable until a secure connection is established.');
}
let appSession = '';
async function request(path, body) {
  if (!appSession) {
    const auth = await fetch('/api/session', { method: 'POST', headers: { 'x-telegram-init-data': app?.initData || '' } });
    const session = await auth.json();
    if (!auth.ok) throw new Error(session.error || 'Authentication unavailable');
    appSession = session.appSession;
  }
  const response = await fetch(path, { method: body ? 'POST' : 'GET', headers: { 'x-miniapp-session': appSession, 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Connection unavailable');
  return data;
}
let loading = false;
async function refresh() {
  if (loading) return;
  loading = true; byId('refresh').disabled = true;
  try {
    if (!app?.initData) throw new Error('Open Superboard from the Telegram bot menu to securely access your workspace.');
    const data = await request('/api/state');
    byId('notice').textContent = '';
    byId('connection').textContent = data.status?.polling ? 'Daemon connected · Bot polling' : 'Daemon connected · Bot polling unavailable';
    for (const id of ['sessions', 'approvals', 'lanes', 'blockers', 'queue']) byId(id).replaceChildren();
    if (!data.sessions) empty('sessions', 'Session host unavailable.');
    else if (!data.sessions.length) empty('sessions', 'No sessions reported by the host.');
    else for (const session of data.sessions) card('sessions', session.title || session.id, session.cwd || session.workspace || '', session.id === data.session ? 'Attached to this chat' : session.id);
    if (!data.approvals) empty('approvals', 'Attach this chat to a session using /attach before reviewing approvals.');
    else if (!data.approvals.length) empty('approvals', 'No pending approvals for the attached session.');
    else for (const approval of data.approvals) {
      const node = card('approvals', approval.requester || 'Approval request', [approval.command, approval.target, approval.reason, approval.task].filter(Boolean).join('\n'), `Expires ${new Date(approval.expiresAt).toLocaleTimeString()}`);
      for (const [text, decision] of [['Yes', 'approved'], ['No', 'denied']]) {
        const button = document.createElement('button'); button.textContent = text; button.disabled = decision === 'approved' && !approval.approvable;
        button.onclick = async () => {
          node.querySelectorAll('button').forEach(b => { b.disabled = true; });
          try { await request('/api/approval', { token: approval.token, decision }); await refresh(); }
          catch (error) { byId('notice').textContent = error.message; await refresh(); }
        };
        node.append(button);
      }
    }
    const snapshot = data.dashboard;
    const stale = !snapshot || Date.now() - snapshot.observedAt > 300000;
    byId('freshness').textContent = stale ? 'Stale / unavailable' : `Reported ${new Date(snapshot.observedAt).toLocaleTimeString()}`;
    for (const [id, key, label] of [['lanes','lanes','Lane'], ['blockers','blockers','Blocker'], ['queue','mergeQueue','Pull request']]) {
      const items = snapshot?.[key];
      if (!Array.isArray(items)) empty(id, `${label} state unavailable.`);
      else if (!items.length) empty(id, stale ? 'No entries in stale snapshot; current state unknown.' : 'No entries reported.');
      else for (const item of items) {
        const node = card(id, item.name || item.title || item.question || label, item.task || '', `${stale ? 'Last reported · ' : ''}${item.state || ''}`);
        if (/^https:\/\/github\.com\//.test(item.url || '')) { const link = document.createElement('a'); link.href = item.url; link.textContent = 'View on GitHub'; link.target = '_blank'; link.rel = 'noopener noreferrer'; node.append(link); }
      }
    }
  } catch (error) { byId('connection').textContent = 'Not connected'; byId('notice').textContent = error.message; unavailable(); }
  finally { loading = false; byId('refresh').disabled = false; }
}
byId('refresh').onclick = refresh;
unavailable(); refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 15000);
