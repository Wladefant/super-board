// Auth glue for the Week view: the mini app's client (Telegram initData -> app session), unchanged.
// The server serves miniapp/client.js at /miniapp/client.js, so there is one copy of the session logic.
export { createClient, isTerminalAuthError, REOPEN_MESSAGE, TerminalAuthError } from '../miniapp/client.js';

export const OPEN_FROM_TELEGRAM = 'Open the Week view from the Superboard bot in Telegram to sign in.';
