export function createClient(initData, transport = fetch) {
  let appSession = '';
  return async function request(path, body) {
    if (!appSession) {
      const auth = await transport('/api/session', { method: 'POST', headers: { 'x-telegram-init-data': initData() } });
      const session = await auth.json();
      if (!auth.ok) throw new Error(session.error || 'Authentication unavailable');
      appSession = session.appSession;
    }
    const response = await transport(path, { method: body ? 'POST' : 'GET', headers: { 'x-miniapp-session': appSession, 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) });
    // Clear before decoding: even a proxy-generated 401 invalidates this session.
    // Never replay mutations: the caller must explicitly retry after authentication.
    if (response.status === 401 || (path === '/api/logout' && response.ok)) appSession = '';
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Connection unavailable');
    return data;
  };
}
