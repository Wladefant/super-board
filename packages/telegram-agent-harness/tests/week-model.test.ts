import { describe, expect, test } from "bun:test";
import {
  ALL, INK, PROJECT_PALETTE, UNASSIGNED, boardOptions, contrastRatio, daySegments, isValidWeek, layoutDay,
  cardsOnDay, mondayOf, resolveSelection, selectBlocks, selectCards, shiftWeek, summarize, unionMs,
} from "../week/week-model.js";
import { BOARDS, makeWeek } from "./fixtures/week-fixture";

const HOUR = 3_600_000;
const week = makeWeek("normal");
const singleSelections = [...BOARDS.map(b => b.id), UNASSIGNED];

describe("board selection (#482 dropdown)", () => {
  test("options list All, one calendar per kind present, every board, and Unassigned", () => {
    const opts = boardOptions(week.boards);
    expect(opts.all.value).toBe(ALL);
    expect(opts.kinds.map((o: { value: string }) => o.value).sort()).toEqual(["kind:ing", "kind:polysimulator", "kind:shipnovo", "kind:veyyon-lanes"]);
    expect(opts.boards.map((o: { value: string }) => o.value).sort()).toEqual(BOARDS.map(b => b.id).sort());
    expect(opts.unassigned.value).toBe(UNASSIGNED);
  });

  test("the URL wins on load, then storage, then All; unknown values never stick", () => {
    expect(resolveSelection("Wladefant/11", "Bavariance/1", BOARDS)).toBe("Wladefant/11");
    expect(resolveSelection("kind:ing", null, BOARDS)).toBe("kind:ing");
    expect(resolveSelection("Nope/9", "Bavariance/1", BOARDS)).toBe("Bavariance/1");
    expect(resolveSelection("kind:unknown", "also-bad", BOARDS)).toBe(ALL);
    expect(resolveSelection(null, null, BOARDS)).toBe(ALL);
  });

  test("each board and kind returns only its own lanes; a lane with no board appears only under Unassigned", () => {
    const projectsOf = (value: string) => [...new Set(selectBlocks(week, value).map((b: { project: string }) => b.project))].sort();
    expect(projectsOf(ALL)).toEqual(["polysimulator", "scratch", "shipnovo", "super-board", "testing", "veyyon"]);
    expect(projectsOf("Wladefant/5")).toEqual(["super-board", "veyyon"]);
    expect(projectsOf("kind:veyyon-lanes")).toEqual(["super-board", "veyyon"]);
    expect(projectsOf("Bavariance/1")).toEqual(["polysimulator"]);
    expect(projectsOf("kind:shipnovo")).toEqual(["shipnovo"]);
    expect(projectsOf(UNASSIGNED)).toEqual(["scratch"]);
    for (const id of BOARDS.map(b => b.id)) expect(projectsOf(id)).not.toContain("scratch");
  });

  test("All equals the sum of the single calendars for lanes, commits and lane time", () => {
    const all = summarize(week, ALL);
    const singles = singleSelections.map(value => summarize(week, value));
    expect(singles.reduce((s, x) => s + x.blocks.length, 0)).toBe(all.blocks.length);
    expect(singles.reduce((s, x) => s + x.commits, 0)).toBe(all.commits);
    expect(singles.reduce((s, x) => s + x.sessionMs, 0)).toBe(all.sessionMs);
    expect(all.blocks.length).toBe(week.totals.blocks);
    expect(all.commits).toBe(week.totals.commits);
  });

  test("cards follow the selection and keep the derived flag for boards without a date field", () => {
    expect(selectCards(week, ALL)).toHaveLength(4);
    const shipnovo = selectCards(week, "Wladefant/11");
    expect(shipnovo).toHaveLength(1);
    expect(shipnovo[0].derived).toBe(true);
    expect(selectCards(week, UNASSIGNED)).toHaveLength(0);
    expect(selectCards({ ...week, cards: undefined }, ALL)).toEqual([]);
  });

  test("a planned range shows on every day it covers; endAt is exclusive", () => {
    const cards = selectCards(week, ALL);
    const perDay = week.days.map(day => cardsOnDay(cards, day).map((c: { title: string }) => c.title));
    perDay.forEach(titles => expect(titles).toContain("Week view: UI with week grid"));
    expect(perDay[2]).toContain("Carrier rate import");
    expect(perDay[3]).not.toContain("Carrier rate import");
    const endsMonday = [{ ...cards[0], endAt: week.days[1].dayStart }];
    expect(week.days.map(day => cardsOnDay(endsMonday, day).length)).toEqual([1, 0, 0, 0, 0, 0, 0]);
  });
});

describe("totals", () => {
  test("parallel time counts once and is clipped to the window", () => {
    expect(unionMs([[0, 10], [5, 15], [20, 25]])).toBe(20);
    expect(unionMs([[0, 10], [0, 10]])).toBe(10);
    expect(unionMs([[0, 10], [10, 20]])).toBe(20);
    expect(unionMs([[0, 100]], 40, 60)).toBe(20);
    expect(unionMs([])).toBe(0);
  });

  test("All matches the server totals for active time, per project and per day", () => {
    const all = summarize(week, ALL);
    expect(all.totalMs).toBe(week.totals.activeMs);
    expect(all.sessionMs).toBe(week.totals.sessionMs);
    expect(all.totalMs).toBeLessThan(all.sessionMs); // the fixture has parallel lanes
    for (const p of week.projects) expect(all.projects.find((x: { project: string }) => x.project === p.project)?.ms).toBe(p.ms);
    expect(all.days.map((d: { ms: number }) => d.ms)).toEqual(week.days.map(d => d.ms));
    expect(all.noCommit.map((b: { sessionId: string; startMs: number }) => `${b.sessionId}:${b.startMs}`).sort()).toEqual([...week.noCommitBlocks].sort());
  });

  test("All shows the server report; a single board gets its own three lines naming a no-commit lane", () => {
    expect(summarize(week, ALL).report).toEqual(week.report);
    const report = summarize(week, "Wladefant/11").report;
    expect(report).toHaveLength(3);
    expect(report[0]).toStartWith("Shipped: 1 merged PR");
    expect(report[1]).toStartWith("Most time: shipnovo");
    expect(report[2]).toContain("Shipment list filters on mobile");
    expect(summarize(week, "Wladefant/5").report[0]).toBe("Shipped: 2 merged PRs, latest Week view data layer.");
  });

  test("unknown commit counts are not counted as commits or as no-commit", () => {
    const unknown = week.blocks.find(b => b.commits === null)!;
    expect(unknown.noCommit).toBe(false);
    expect(summarize(week, ALL).noCommit).not.toContain(unknown);
  });
});

describe("grid layout", () => {
  test("a lane past midnight shows on both days", () => {
    const late = week.blocks.find(b => b.sessionId === "veyyon-2-10")!;
    const segs = daySegments([late], week.days);
    expect(segs[2]).toHaveLength(1);
    expect(segs[3]).toHaveLength(1);
    expect(segs[2][0].endMs).toBe(week.days[2].dayEnd);
    expect(segs[3][0].startMs).toBe(week.days[3].dayStart);
  });

  test("non-overlapping lanes take the full width", () => {
    const { placed, overflow } = layoutDay([{ startMs: 0, endMs: HOUR }, { startMs: HOUR, endMs: 2 * HOUR }]);
    expect(placed.map((p: { columns: number }) => p.columns)).toEqual([1, 1]);
    expect(overflow).toEqual([]);
  });

  test("40 lanes in one day stay within 4 columns and every hidden lane sits behind a +N control", () => {
    const busy = makeWeek("busy");
    const day = daySegments(busy.blocks, busy.days)[2];
    expect(day.length).toBeGreaterThanOrEqual(40);
    const { placed, overflow } = layoutDay(day);
    expect(Math.max(...placed.map((p: { columns: number }) => p.columns))).toBeLessThanOrEqual(4);
    expect(placed.every((p: { column: number; columns: number }) => p.column < p.columns)).toBe(true);
    const hidden = overflow.reduce((s: number, o: { segments: unknown[] }) => s + o.segments.length, 0);
    expect(placed.length + hidden).toBe(day.length);
    expect(overflow.length).toBeGreaterThan(0);
  });
});

describe("week navigation and colour", () => {
  test("weeks start on Monday and move by seven days", () => {
    expect(mondayOf(new Date(2026, 9, 4))).toBe("2026-09-28"); // Sunday
    expect(mondayOf(new Date(2026, 8, 28))).toBe("2026-09-28"); // Monday
    expect(shiftWeek("2026-09-28", 1)).toBe("2026-10-05");
    expect(shiftWeek("2026-10-26", -1)).toBe("2026-10-19"); // across the DST change
    expect(isValidWeek("2026-09-28")).toBe(true);
    expect(isValidWeek("2026-09-29")).toBe(false);
    expect(isValidWeek("2026-02-30")).toBe(false);
    expect(isValidWeek(null)).toBe(false);
  });

  test("every project colour keeps 4.5:1 contrast with the block text", () => {
    for (const color of PROJECT_PALETTE) expect(contrastRatio(color, INK)).toBeGreaterThanOrEqual(4.5);
  });
});
