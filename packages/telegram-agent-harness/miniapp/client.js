export const UNAVAILABLE_MESSAGE = 'Unavailable until a secure connection is established.';
export const REOPEN_MESSAGE = 'Open this app from Telegram again to authenticate.';
export const SECTIONS = ['sessions', 'lanes', 'blockers', 'queue'];

export function getUnavailableState(notice = REOPEN_MESSAGE) {
  return {
    connection: 'Not connected',
    notice: notice || REOPEN_MESSAGE,
    freshness: 'Unavailable',
    sections: {
      sessions: UNAVAILABLE_MESSAGE,
      lanes: UNAVAILABLE_MESSAGE,
      blockers: UNAVAILABLE_MESSAGE,
      queue: UNAVAILABLE_MESSAGE,
    },
  };
}

export class TerminalAuthError extends Error {
  constructor(message = REOPEN_MESSAGE) {
    super(message);
    this.name = 'TerminalAuthError';
    this.isTerminalAuth = true;
  }
}

export function isTerminalAuthError(error) {
  return error instanceof TerminalAuthError || error?.name === 'TerminalAuthError' || Boolean(error?.isTerminalAuth);
}

export function createClient(getInitData, transport = fetch) {
  let appSession = '';

  async function obtainSession() {
    const rawInit = typeof getInitData === 'function' ? getInitData() : (getInitData || '');
    const auth = await transport('/api/session', {
      method: 'POST',
      headers: { 'x-telegram-init-data': rawInit || '' },
    });
    let data;
    try {
      data = await auth.json();
    } catch {
      data = {};
    }
    if (!auth.ok || !data?.appSession) {
      appSession = '';
      throw new TerminalAuthError(data?.error || REOPEN_MESSAGE);
    }
    appSession = data.appSession;
    return appSession;
  }

  async function send(path, body) {
    if (!appSession && path !== '/api/session') {
      await obtainSession();
    }
    return transport(path, {
      method: body ? 'POST' : 'GET',
      headers: {
        ...(appSession ? { 'x-miniapp-session': appSession } : {}),
        'Content-Type': 'application/json',
      },
      ...(body ? { body: typeof body === 'string' ? body : JSON.stringify(body) } : {}),
    });
  }

  const client = async function request(path, body) {
    let response = await send(path, body);

    if (response.status === 401 && path !== '/api/session') {
      // 401 on protected endpoint: drop cached session token,
      // re-run /api/session with fresh Telegram.WebApp.initData,
      // retry the original request once.
      appSession = '';
      await obtainSession();
      response = await send(path, body);
      if (response.status === 401) {
        appSession = '';
      }
    }

    let data;
    try {
      data = await response.json();
    } catch {
      data = {};
    }

    if (!response.ok) {
      if (response.status === 401) {
        throw new TerminalAuthError(data?.error || REOPEN_MESSAGE);
      }
      throw new Error(data?.error || 'Connection unavailable');
    }

    return data;
  };

  client.getSession = () => appSession;
  client.clearSession = () => { appSession = ''; };
  client.setSession = (s) => { appSession = s; };

  return client;
}
