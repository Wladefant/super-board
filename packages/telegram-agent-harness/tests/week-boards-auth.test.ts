import type { Server } from "bun";
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { createHmac } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { miniAppRequest } from "../daemon/miniapp";
import { readBoardsFile } from "../daemon/week-service";
import { resolveWeekFile, startWeekServer } from "../week/server";

const secret = "w".repeat(64);
const token = "123456:test-token";
const users = ["1247617658"];

function signedInitData(botToken: string): string {
  const params = new URLSearchParams({ auth_date: String(Math.floor(Date.now() / 1000)), user: JSON.stringify({ id: 1247617658 }) });
  const key = createHmac("sha256", "WebAppData").update(botToken).digest();
  params.set("hash", createHmac("sha256", key).update([...params].map(([k, v]) => `${k}=${v}`).join("\n")).digest("hex"));
  return params.toString();
}

describe("GET /boards.json on the week server", () => {
  let server: Server<undefined>;
  let ws: WebSocket;
  let dir: string;
  let base: string;
  let appSession: string;

  beforeAll(async () => {
    dir = mkdtempSync(join(tmpdir(), "week-boards-auth-"));
    server = startWeekServer(secret, 0);
    base = `http://localhost:${server.port}`;
    ws = new WebSocket(`ws://localhost:${server.port}/relay`, { headers: { Authorization: `Bearer ${secret}` } });
    await new Promise<void>((resolve, reject) => { ws.onopen = () => resolve(); ws.onerror = reject; });
    ws.onmessage = async event => {
      ws.send(JSON.stringify(await miniAppRequest(JSON.parse(String(event.data)), {
        stateDir: dir, token, allowedUsers: users, session: () => null,
        sessions: async () => [], dashboard: () => null, status: () => ({}), boards: readBoardsFile,
      })));
    };
    const exchange = await fetch(`${base}/api/session`, { method: "POST", headers: { "x-telegram-init-data": signedInitData(token) } });
    expect(exchange.status).toBe(200);
    appSession = (await exchange.json()).appSession;
  });

  afterAll(() => { ws.close(); server.stop(true); rmSync(dir, { recursive: true, force: true }); });

  test("401 without auth and no board data in the body", async () => {
    const response = await fetch(`${base}/boards.json`);
    expect(response.status).toBe(401);
    const body = await response.text();
    expect(body).not.toContain("Bavariance");
    expect(body).not.toContain("boards");
  });

  test("200 with a valid app session returns the board list", async () => {
    const response = await fetch(`${base}/boards.json`, { headers: { "x-miniapp-session": appSession } });
    expect(response.status).toBe(200);
    const data = await response.json();
    expect(data.boards.length).toBeGreaterThan(0);
    expect(data.boards[0].repos.length).toBeGreaterThan(0);
  });

  test("401 with tampered initData, and with a tampered app session", async () => {
    const tampered = signedInitData(token).replace(/hash=[a-f0-9]{4}/, "hash=0000");
    const viaInitData = await fetch(`${base}/boards.json`, { headers: { "x-telegram-init-data": tampered } });
    expect(viaInitData.status).toBe(401);
    expect(await viaInitData.text()).not.toContain("Bavariance");
    const exchange = await fetch(`${base}/api/session`, { method: "POST", headers: { "x-telegram-init-data": tampered } });
    expect(exchange.status).toBe(401);
    const forged = appSession.slice(0, -4) + (appSession.endsWith("0000") ? "1111" : "0000");
    const viaSession = await fetch(`${base}/boards.json`, { headers: { "x-miniapp-session": forged } });
    expect(viaSession.status).toBe(401);
    expect(await viaSession.text()).not.toContain("Bavariance");
  });

  test("the static allow-list no longer serves any json file", () => {
    expect(resolveWeekFile("/boards.json")).toBeUndefined();
    expect(resolveWeekFile("/week.js")).toBe("week.js");
  });
});
