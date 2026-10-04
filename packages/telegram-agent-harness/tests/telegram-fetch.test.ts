import { describe, expect, test } from "bun:test";
import { orderAddresses, telegramFetch, type ResolvedAddress } from "../extension/telegram-fetch";

const BOTH: ResolvedAddress[] = [
  { address: "2001:67c:4e8:f004::9", family: 6 },
  { address: "149.154.166.110", family: 4 },
];

interface Call { url: string; host: string | null; serverName?: string }

function recorder(outcomes: Array<Error | "ok">) {
  const calls: Call[] = [];
  const fetch = async (url: string, init: RequestInit) => {
    const tls = (init as { tls?: { serverName?: string } }).tls;
    calls.push({ url, host: new Headers(init.headers).get("Host"), serverName: tls?.serverName });
    const outcome = outcomes[calls.length - 1] ?? "ok";
    if (outcome !== "ok") throw outcome;
    return new Response("{}");
  };
  return { calls, fetch };
}

function connectError(code: string): Error {
  return Object.assign(new TypeError("Unable to connect"), { code });
}

describe("telegramFetch address family", () => {
  test("orders IPv4 before IPv6 regardless of resolver order", () => {
    expect(orderAddresses(BOTH).map((a) => a.family)).toEqual([4, 6]);
  });

  test("connects to the IPv4 address first when both families resolve, keeping Host and SNI", async () => {
    const { calls, fetch } = recorder(["ok"]);
    await telegramFetch("https://api.telegram.org/bot1/getMe?x=1", { method: "POST" }, { resolve: async () => BOTH, fetch });
    expect(calls).toEqual([
      { url: "https://149.154.166.110/bot1/getMe?x=1", host: "api.telegram.org", serverName: "api.telegram.org" },
    ]);
  });

  test("falls back to IPv6 only after the IPv4 connect fails", async () => {
    const { calls, fetch } = recorder([connectError("ConnectionRefused"), "ok"]);
    const res = await telegramFetch("https://api.telegram.org/bot1/getMe", {}, { resolve: async () => BOTH, fetch });
    expect(res.ok).toBe(true);
    expect(calls.map((c) => c.url)).toEqual([
      "https://149.154.166.110/bot1/getMe",
      "https://[2001:67c:4e8:f004::9]/bot1/getMe",
    ]);
    expect(calls[1].host).toBe("api.telegram.org");
  });

  test("works on an IPv6-only network", async () => {
    const { calls, fetch } = recorder(["ok"]);
    await telegramFetch("https://api.telegram.org/bot1/getMe", {}, { resolve: async () => [BOTH[0]], fetch });
    expect(calls.map((c) => c.url)).toEqual(["https://[2001:67c:4e8:f004::9]/bot1/getMe"]);
  });

  test("does not fall back after a timeout, which may have delivered the request", async () => {
    const timeout = Object.assign(new Error("timed out"), { name: "TimeoutError", code: 23 });
    const { calls, fetch } = recorder([timeout]);
    await expect(telegramFetch("https://api.telegram.org/bot1/x", {}, { resolve: async () => BOTH, fetch })).rejects.toBe(timeout);
    expect(calls).toHaveLength(1);
  });

  test("throws the last connect error when every address fails", async () => {
    const last = connectError("ENETUNREACH");
    const { calls, fetch } = recorder([connectError("ConnectionRefused"), last]);
    await expect(telegramFetch("https://api.telegram.org/bot1/x", {}, { resolve: async () => BOTH, fetch })).rejects.toBe(last);
    expect(calls).toHaveLength(2);
  });

  test("uses the plain host name when resolution fails", async () => {
    const { calls, fetch } = recorder(["ok"]);
    await telegramFetch("https://api.telegram.org/bot1/x", {}, { resolve: async () => { throw new Error("dns down"); }, fetch });
    expect(calls.map((c) => c.url)).toEqual(["https://api.telegram.org/bot1/x"]);
    expect(calls[0].serverName).toBeUndefined();
  });
});
