// The Week page takes its colours from Telegram's theme and follows a theme change while it is open;
// outside Telegram it follows prefers-color-scheme (Refs #562).
//
// Loads the real `week/index.html` and `week/week.js` into happy-dom. The Telegram stand-in has the
// parts of telegram-web-app.js the page uses: themeParams, colorScheme, onEvent and the header colours.
import { afterEach, describe, expect, test } from "bun:test";
import { openWeekPage, type WeekPage } from "./fixtures/week-dom";

const LIGHT = { bg_color: "#ffffff", text_color: "#000000", hint_color: "#999999", link_color: "#2481cc", secondary_bg_color: "#efeff3", section_bg_color: "#ffffff", section_separator_color: "#c8c7cc" };
const DARK = { bg_color: "#000000", text_color: "#ffffff", hint_color: "#98989e", link_color: "#3e88f7", secondary_bg_color: "#1c1c1d", section_bg_color: "#2c2c2e", section_separator_color: "#545458" };

const signedOut = () => Response.json({ error: "Missing Telegram init data." }, { status: 401 });

function telegram(themeParams: Record<string, string>, colorScheme: "light" | "dark") {
  const handlers: Record<string, (() => void)[]> = {};
  const calls: string[] = [];
  return {
    initData: "", themeParams, colorScheme, version: "8.0",
    ready() {}, expand() {},
    isVersionAtLeast: (v: string) => Number(v) <= 8,
    onEvent(name: string, fn: () => void) { (handlers[name] ||= []).push(fn); },
    setHeaderColor(color: string) { calls.push(`header ${color}`); },
    setBackgroundColor(color: string) { calls.push(`background ${color}`); },
    /** What telegram-web-app.js does on theme_changed: new params, then the themeChanged handlers. */
    changeTheme(params: Record<string, string>, scheme: "light" | "dark") {
      this.themeParams = params;
      this.colorScheme = scheme;
      for (const fn of handlers.themeChanged || []) fn.call(this);
    },
    calls,
  };
}

let page: WeekPage | null = null;
afterEach(async () => { await page?.close(); page = null; });

const root = () => page!.window.document.documentElement;
const token = (name: string) => root().style.getPropertyValue(name);

describe("in Telegram", () => {
  test("the page takes Telegram's colours and scheme, and follows a theme change while open", async () => {
    const tg = telegram(LIGHT, "light");
    page = await openWeekPage(signedOut, "/", { telegram: tg, prefersColorScheme: "dark" });
    // Telegram's scheme wins over the system's.
    expect(root().dataset.scheme).toBe("light");
    expect(token("--bg")).toBe("#efeff3");
    expect(token("--panel")).toBe("#ffffff");
    expect(token("--text")).toBe("#000000");
    expect(tg.calls).toEqual(["header #efeff3", "background #efeff3"]);

    tg.changeTheme(DARK, "dark");
    expect(root().dataset.scheme).toBe("dark");
    expect(token("--bg")).toBe("#1c1c1d");
    expect(token("--panel")).toBe("#2c2c2e");
    expect(tg.calls.slice(2)).toEqual(["header #1c1c1d", "background #1c1c1d"]);
    expect(page!.window.document.querySelector('meta[name="theme-color"]')!.getAttribute("content")).toBe("#1c1c1d");
  });

  test("an unreadable Telegram theme falls back to the page's own palette in Telegram's scheme", async () => {
    page = await openWeekPage(signedOut, "/", { telegram: telegram({ bg_color: "#ffffff", text_color: "#eeeeee" }, "light") });
    expect(root().dataset.scheme).toBe("light");
    expect(token("--bg")).toBe("");
    expect(token("--text")).toBe("");
  });
});

describe("outside Telegram", () => {
  test.each(["light", "dark"] as const)("the page follows prefers-color-scheme: %s", async scheme => {
    page = await openWeekPage(signedOut, "/", { prefersColorScheme: scheme });
    expect(root().dataset.scheme).toBe(scheme);
    expect(token("--bg")).toBe("");
  });

  test("telegram-web-app.js loaded in a plain browser (no theme, colorScheme 'light') does not override a dark system", async () => {
    const tg = telegram({}, "light");
    page = await openWeekPage(signedOut, "/", { telegram: tg, prefersColorScheme: "dark" });
    expect(root().dataset.scheme).toBe("dark");
    expect(token("--bg")).toBe("");
    expect(tg.calls).toEqual([]);
  });
});
