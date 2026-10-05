// Week view colours (Refs #562): both schemes in week.css, and the Telegram theme mapped onto them,
// keep WCAG AA. Text 4.5:1; control borders, lane fills, the status ring and the focus ring 3:1.
// The colours come from the shipped week.css, so a palette edit that breaks a pair fails here.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { contrastRatio, telegramTokens } from "../week/week-model.js";

const css = readFileSync(new URL("../week/week.css", import.meta.url), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");

/** The custom properties declared in the first `<selector> {` block of week.css. */
function tokens(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`week.css has no "${selector}" block`);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
  return Object.fromEntries([...body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map(m => [m[1], m[2].trim()]));
}

const DARK = tokens(":root");
const LIGHT = { ...DARK, ...tokens(':root[data-scheme="light"]') };
const LANES = Object.keys(DARK).filter(name => /^--lane-\d+$/.test(name));

/** `#rrggbb`, or `rgb(r g b / a)` painted over `surface` (#rrggbb). */
function solid(value: string, surface: string): string {
  if (/^#[0-9a-f]{6}$/i.test(value)) return value;
  const m = /^rgb\((\d+) (\d+) (\d+) \/ ([\d.]+)\)$/.exec(value);
  if (!m) throw new Error(`not a colour this test can read: ${value}`);
  const alpha = Number(m[4]);
  const under = [1, 3, 5].map(i => parseInt(surface.slice(i, i + 2), 16));
  return `#${[m[1], m[2], m[3]].map((c, i) => Math.round(Number(c) * alpha + under[i] * (1 - alpha)).toString(16).padStart(2, "0")).join("")}`;
}

/** Every pair below its minimum, as readable lines; [] when the palette passes. */
function failures(t: Record<string, string>): string[] {
  const out: string[] = [];
  const need = (fg: string, bg: string, min: number, tint?: string) => {
    const back = tint ? solid(t[tint], t[bg]) : t[bg];
    const ratio = contrastRatio(solid(t[fg], back), back);
    if (!(ratio >= min)) out.push(`${fg} on ${tint ? `${tint} over ` : ""}${bg}: ${ratio.toFixed(2)} < ${min}`);
  };
  const surfaces = ["--bg", "--panel", "--panel-2"];
  for (const fg of ["--text", "--muted", "--accent"]) for (const bg of surfaces) need(fg, bg, 4.5);
  for (const bg of ["--btn", "--btn-hover"]) need("--text", bg, 4.5);
  for (const bg of ["--panel", "--panel-2"]) need("--red-text", bg, 4.5);
  need("--red-text", "--bg", 4.5, "--red-tint");
  need("--amber", "--bg", 4.5, "--amber-tint");
  for (const bg of surfaces) need("--ctl-line", bg, 3);
  for (const bg of ["--bg", "--panel"]) for (const fg of ["--focus", "--red", "--red-edge", "--amber-edge"]) need(fg, bg, 3);
  for (const lane of LANES) {
    need("--ink", lane, 4.5);
    for (const bg of ["--bg", "--panel"]) need(lane, bg, 3);
  }
  return out;
}

// Telegram's own default themes (Telegram.WebApp.themeParams as the iOS client sends them).
const TELEGRAM_LIGHT = {
  bg_color: "#ffffff", text_color: "#000000", hint_color: "#999999", link_color: "#2481cc", button_color: "#2481cc",
  button_text_color: "#ffffff", secondary_bg_color: "#efeff3", header_bg_color: "#f8f8f8", accent_text_color: "#2481cc",
  section_bg_color: "#ffffff", section_header_text_color: "#6d6d72", subtitle_text_color: "#999999",
  destructive_text_color: "#ff3b30", section_separator_color: "#c8c7cc",
};
const TELEGRAM_DARK = {
  bg_color: "#000000", text_color: "#ffffff", hint_color: "#98989e", link_color: "#3e88f7", button_color: "#3e88f7",
  button_text_color: "#ffffff", secondary_bg_color: "#1c1c1d", header_bg_color: "#1a1a1a", accent_text_color: "#3e88f7",
  section_bg_color: "#2c2c2e", section_header_text_color: "#8d8e93", subtitle_text_color: "#98989e",
  destructive_text_color: "#eb5545", section_separator_color: "#545458",
};

describe("week.css schemes", () => {
  test("declares ten lane colours and a light scheme that differs from the dark one", () => {
    expect(LANES.length).toBe(10);
    expect(LIGHT["--bg"]).not.toBe(DARK["--bg"]);
    expect(LIGHT["--text"]).not.toBe(DARK["--text"]);
    expect(LANES.filter(lane => LIGHT[lane] !== DARK[lane]).length).toBe(LANES.length);
  });

  test("the dark scheme keeps AA contrast", () => expect(failures(DARK)).toEqual([]));
  test("the light scheme keeps AA contrast", () => expect(failures(LIGHT)).toEqual([]));
});

describe("Telegram theme", () => {
  test.each([
    ["light", TELEGRAM_LIGHT, LIGHT],
    ["dark", TELEGRAM_DARK, DARK],
  ])("Telegram's default %s theme sets the surfaces and keeps AA contrast", (_, params, scheme) => {
    const theme = telegramTokens(params)!;
    expect(theme["--bg"]).toBe(params.secondary_bg_color);
    expect(theme["--panel"]).toBe(params.section_bg_color);
    expect(theme["--text"]).toBe(params.text_color);
    expect(theme["--line"]).toBe(params.section_separator_color);
    expect(failures({ ...scheme, ...theme })).toEqual([]);
  });

  test("hint and link colours too faint to read are pulled toward the text colour until they reach 4.5:1", () => {
    const faint = { bg_color: "#ffffff", text_color: "#111111", hint_color: "#f2f2f2", link_color: "#fafafa" };
    const theme = telegramTokens(faint)!;
    expect(contrastRatio(theme["--muted"], "#ffffff")).toBeGreaterThanOrEqual(4.5);
    expect(contrastRatio(theme["--accent"], "#ffffff")).toBeGreaterThanOrEqual(4.5);
    expect(failures({ ...LIGHT, ...theme })).toEqual([]);
  });

  test("a readable hint colour is kept as Telegram sends it", () => {
    expect(telegramTokens({ ...TELEGRAM_DARK, hint_color: "#aeaeb2" })!["--muted"]).toBe("#aeaeb2");
  });

  test("a theme whose text does not read on its background is not used", () => {
    expect(telegramTokens({ bg_color: "#ffffff", text_color: "#dddddd" })).toBeNull();
  });

  test("outside Telegram (no theme params) there is no Telegram theme", () => {
    expect(telegramTokens({})).toBeNull();
    expect(telegramTokens(undefined)).toBeNull();
  });
});
