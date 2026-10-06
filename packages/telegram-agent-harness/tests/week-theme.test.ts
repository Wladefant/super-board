// Week view colours (Refs #562): both schemes in week.css, and the Telegram theme mapped onto them,
// keep WCAG AA. Text 4.5:1; control borders, lane fills, the status ring and the focus ring 3:1.
// --line is not checked: it is decorative (see the head of week.css).
// The colours come from the shipped week.css, so a palette edit that breaks a pair fails here.
import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { contrastRatio, mixColors, telegramTokens } from "../week/week-model.js";

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

  test("every board colour in boards.json, as the card chip shows it, keeps 3:1 on the surfaces of both schemes", () => {
    const boards: { id: string; color: string }[] = JSON.parse(readFileSync(new URL("../week/boards.json", import.meta.url), "utf8")).boards;
    expect(boards.length).toBeGreaterThan(0);
    const out: string[] = [];
    for (const [name, t] of [["dark", DARK], ["light", LIGHT]] as const) {
      const tone = Number.parseFloat(t["--board-tone"]) / 100;
      for (const board of boards) {
        const shown = mixColors(t["--text"], board.color, tone);
        for (const bg of ["--bg", "--panel"]) {
          const ratio = contrastRatio(shown, t[bg]);
          if (!(ratio >= 3)) out.push(`${name}: ${board.id} ${board.color} on ${bg}: ${ratio.toFixed(2)}`);
        }
      }
    }
    expect(out).toEqual([]);
  });
});

describe("Telegram theme", () => {
  test.each([
    ["light", TELEGRAM_LIGHT, LIGHT],
    ["dark", TELEGRAM_DARK, DARK],
  ])("Telegram's default %s theme sets the surfaces and keeps AA contrast", (_, params, scheme) => {
    const theme = telegramTokens(params, scheme)!;
    expect(theme["--bg"]).toBe(params.secondary_bg_color);
    expect(theme["--panel"]).toBe(params.section_bg_color);
    expect(theme["--text"]).toBe(params.text_color);
    expect(theme["--line"]).toBe(params.section_separator_color);
    expect(failures({ ...scheme, ...theme })).toEqual([]);
  });

  // Custom themes users can pick, at the edges of what still reads: Telegram's text stays 4.5:1 on them,
  // but the scheme's own status and lane colours would not keep AA without being moved.
  const CUSTOM: [string, Record<string, string>, Record<string, string>][] = [
    ["grey-blue light", { bg_color: "#c0c8d0", secondary_bg_color: "#c0c8d0", section_bg_color: "#c8d0d8", text_color: "#000000", hint_color: "#333333", link_color: "#1a4fa0" }, LIGHT],
    ["warm light", { bg_color: "#e8d9b5", secondary_bg_color: "#dcc9a0", section_bg_color: "#efe3c6", text_color: "#1a1206", hint_color: "#5c4b2a", link_color: "#7a3d00" }, LIGHT],
    ["mid-grey dark", { bg_color: "#4a4f58", secondary_bg_color: "#40454d", section_bg_color: "#4a4f58", text_color: "#ffffff", hint_color: "#d0d4da", link_color: "#cfe0ff" }, DARK],
    ["tinted dark", { bg_color: "#18222d", secondary_bg_color: "#131a22", section_bg_color: "#1f2b38", text_color: "#f5f5f5", hint_color: "#7e8b99", link_color: "#62bcf9" }, DARK],
  ];

  test.each(CUSTOM)("custom %s theme: status and lane colours are moved until every pair keeps AA", (_, params, scheme) => {
    expect(failures({ ...scheme, ...telegramTokens(params, scheme)! })).toEqual([]);
  });

  test("the custom themes above do break AA when the scheme's colours are not moved", () => {
    const unmoved = CUSTOM.flatMap(([, params, scheme]) => failures({ ...scheme, ...telegramTokens(params)! }));
    expect(unmoved.length).toBeGreaterThan(0);
  });

  test("a colour that already keeps AA on Telegram's surfaces is kept as week.css has it", () => {
    const theme = telegramTokens(TELEGRAM_DARK, DARK)!;
    for (const lane of LANES) expect(theme[lane]).toBe(DARK[lane]);
    expect(theme["--red-text"]).toBe(DARK["--red-text"]);
  });

  // Refs https://github.com/Wladefant/super-board/issues/611: the red and amber status text sits on its own
  // tint. Random themes (seeded, so a failure reproduces) cover the edges a hand-picked list misses.
  test("on random readable themes, red and amber keep AA on their tints and on the surfaces", () => {
    let seed = 611;
    const random = () => {
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const hex = () => `#${Math.floor(random() * 0x1000000).toString(16).padStart(6, "0")}`;
    let readable = 0;
    const out: string[] = [];
    for (let i = 0; i < 20000; i += 1) {
      const params = { bg_color: hex(), secondary_bg_color: hex(), section_bg_color: hex(), text_color: hex(), hint_color: hex(), link_color: hex() };
      for (const scheme of [LIGHT, DARK]) {
        const theme = telegramTokens(params, scheme);
        if (!theme) continue;
        readable += 1;
        for (const failure of failures({ ...scheme, ...theme }).filter(line => /^--(red|amber)/.test(line))) {
          out.push(`${JSON.stringify(params)} ${scheme === LIGHT ? "light" : "dark"}: ${failure}`);
        }
      }
    }
    expect(readable).toBeGreaterThan(500);
    expect(out.slice(0, 5)).toEqual([]);
  });

  test("Telegram's default themes keep the red and amber colours they had before the tint check", () => {
    const pick = (t: Record<string, string>) => Object.fromEntries(Object.entries(t).filter(([name]) => /^--(red|amber)/.test(name)));
    expect(pick(telegramTokens(TELEGRAM_LIGHT, LIGHT)!)).toEqual({
      "--red": "#c62828", "--red-edge": "#c62828", "--red-tint": "#ebdbdf", "--red-text": "#a61b1b",
      "--amber": "#8a5300", "--amber-edge": "#8a5300", "--amber-tint": "#e5dfdb",
    });
    expect(pick(telegramTokens(TELEGRAM_DARK, DARK)!)).toEqual({
      "--red": "#ff6b6b", "--red-edge": "#ff6b6b", "--red-tint": "#332425", "--red-text": "#ffb4b4",
      "--amber": "#f5c06b", "--amber-edge": "#f5c06b", "--amber-tint": "#322c25",
    });
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
