/**
 * fetch for api.telegram.org that connects over IPv4 first.
 *
 * api.telegram.org's IPv6 route intermittently stalls a connect for ~21 s before the client falls back
 * to IPv4. Bun's `fetch` has no address-family option, so this resolves the A and AAAA records itself,
 * connects to the literal IPv4 address, and keeps the original host through the `Host` header and the
 * TLS server name (certificate verification still runs against the real host name). IPv6 is tried only
 * when every IPv4 connect failed before a byte of the request was sent.
 */
import { lookup as dnsLookup } from "node:dns/promises";

export interface ResolvedAddress {
  address: string;
  family: number;
}

export interface TelegramFetchDeps {
  resolve?: (host: string) => Promise<ResolvedAddress[]>;
  fetch?: (url: string, init: RequestInit) => Promise<Response>;
}

const CONNECT_ERROR_CODES = new Set([
  "ConnectionRefused", "FailedToOpenSocket", "ENOTFOUND", "EAI_AGAIN", "ECONNREFUSED", "ENETUNREACH", "EHOSTUNREACH",
]);

function isConnectFailure(err: unknown): boolean {
  return typeof err === "object" && err !== null && "code" in err
    && typeof err.code === "string" && CONNECT_ERROR_CODES.has(err.code);
}

const defaultResolve = (host: string): Promise<ResolvedAddress[]> => dnsLookup(host, { all: true });

const defaultFetch = (url: string, init: RequestInit): Promise<Response> => fetch(url, init);

/** Addresses to try, IPv4 before IPv6, each family in resolver order. */
export function orderAddresses(addresses: readonly ResolvedAddress[]): ResolvedAddress[] {
  return [...addresses.filter((a) => a.family === 4), ...addresses.filter((a) => a.family === 6)];
}

export async function telegramFetch(
  input: string,
  init: RequestInit = {},
  deps: TelegramFetchDeps = {},
): Promise<Response> {
  const doFetch = deps.fetch ?? defaultFetch;
  const target = new URL(input);
  let addresses: ResolvedAddress[];
  try {
    addresses = orderAddresses(await (deps.resolve ?? defaultResolve)(target.hostname));
  } catch {
    return doFetch(input, init);
  }
  if (addresses.length === 0) return doFetch(input, init);

  let lastError: unknown;
  for (const { address, family } of addresses) {
    const ip = family === 6 ? `[${address}]` : address;
    const url = `${target.protocol}//${ip}${target.port ? `:${target.port}` : ""}${target.pathname}${target.search}`;
    const headers = new Headers(init.headers);
    headers.set("Host", target.host);
    try {
      return await doFetch(url, { ...init, headers, tls: { serverName: target.hostname } } as RequestInit);
    } catch (err) {
      if (!isConnectFailure(err) || init.signal?.aborted) throw err;
      lastError = err;
    }
  }
  throw lastError;
}
