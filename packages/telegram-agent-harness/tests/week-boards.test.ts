import { describe, expect, test } from "bun:test";
import { buildCardsQuery, CardsReader, filterWeek, parseCards, placeCards } from "../daemon/week-boards";
import { GithubReader, type GithubReaderDeps } from "../daemon/week-github";
import { summarizeWeek, weekStartOf, type BoardInfo, type WeekBlock } from "../daemon/week-summary";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const T0 = Date.UTC(2026, 9, 5, 8, 0); // Monday 2026-10-05 08:00Z
const WEEK_START = weekStartOf(T0, "UTC");
const WEEK_END = WEEK_START + 7 * DAY;

const BOARDS: BoardInfo[] = [
  { id: "Wladefant/5", title: "Superboard", kind: "veyyon-lanes", color: "#22d3ee", repos: ["Wladefant/super-board"], owner: "Wladefant", number: 5, dateField: "Target Date" },
  { id: "Bavariance/1", title: "Polysimulator", kind: "polysimulator", color: "#6366f1", repos: ["Bavariance/polysimulator"], owner: "Bavariance", number: 1, dateField: "Target Date" },
  { id: "Bavariance/11", title: "Shipnovo", kind: "shipnovo", color: "#f59e0b", repos: ["Bavariance/shipnovo"], owner: "Bavariance", number: 11, dateField: null },
];

function issue(repo: string, number: number, extra: Record<string, unknown> = {}) {
  return { __typename: "Issue", number, title: `${repo}#${number}`, url: `https://github.com/${repo}/issues/${number}`, state: "OPEN", closedAt: null, repository: { nameWithOwner: repo }, ...extra };
}
const date = (name: string, value: string) => ({ __typename: "ProjectV2ItemFieldDateValue", date: value, field: { name } });
const iteration = (startDate: string, duration: number) => ({ __typename: "ProjectV2ItemFieldIterationValue", startDate, duration, field: { name: "Iteration" } });
const iso = (ms: number) => new Date(ms).toISOString();

// Each item is [content, field values, updatedAt].
function board(items: [Record<string, unknown> | null, unknown[], number][], total = items.length) {
  const nodes = items.map(([content, values, updatedAt]) => ({ updatedAt: iso(updatedAt), content, fieldValues: { nodes: values } }));
  return { items: { totalCount: total, nodes } };
}

const OLD = WEEK_START - 20 * DAY;
const MID = WEEK_START + 2 * DAY;

/** The GraphQL answer for each owner, keyed the way buildCardsQuery aliases it (b0, b1, ...). */
const RESPONSES: Record<string, unknown> = {
  Wladefant: { data: { repositoryOwner: { b0: board([
    [issue("Wladefant/super-board", 1), [date("Target Date", "2026-10-07")], OLD],            // planned in the week
    [issue("Wladefant/super-board", 2), [iteration("2026-09-28", 14)], OLD],                  // iteration overlaps the week
    [issue("Wladefant/super-board", 3), [date("Target Date", "2026-08-01")], OLD],            // planned long ago, no activity: absent
  ]) } } },
  Bavariance: { data: { repositoryOwner: {
    b0: board([
      [issue("Bavariance/polysimulator", 10), [date("Start date", "2026-10-06"), date("Target Date", "2026-10-09")], OLD], // range
      [issue("Bavariance/polysimulator", 11), [date("Target Date", "2026-12-01")], MID],     // planned later, touched in the week: derived
    ]),
    b1: board([
      [issue("Bavariance/shipnovo", 20), [], MID],                                           // no date field, activity: derived
      [issue("Bavariance/shipnovo", 21, { state: "CLOSED", closedAt: iso(MID) }), [], OLD],  // closed in the week: derived
      [issue("Bavariance/shipnovo", 22), [], OLD],                                           // no date field, no activity: absent
      [null, [], MID],                                                                       // redacted item: skipped
    ], 450),
  } } },
};

function raw() {
  const all = [
    ...parseCards(RESPONSES.Wladefant, [BOARDS[0]!]).cards,
    ...parseCards(RESPONSES.Bavariance, [BOARDS[1]!, BOARDS[2]!]).cards,
  ];
  return all;
}

describe("card placement", () => {
  const cards = placeCards(raw(), BOARDS, WEEK_START, WEEK_END);
  const byKey = new Map(cards.map(c => [`${c.boardId}#${c.number}`, c]));

  test("planned dates place a card; activity places the rest and says so", () => {
    expect([...byKey.keys()].sort()).toEqual([
      "Bavariance/1#10", "Bavariance/1#11", "Bavariance/11#20", "Bavariance/11#21", "Wladefant/5#1", "Wladefant/5#2",
    ]);
    expect(byKey.get("Wladefant/5#1")).toMatchObject({ source: "target", derived: false, kind: "veyyon-lanes" });
    expect(byKey.get("Wladefant/5#2")).toMatchObject({ source: "iteration", derived: false });
    expect(byKey.get("Bavariance/1#10")).toMatchObject({ source: "target", derived: false });
    expect(byKey.get("Bavariance/1#10")!.endAt).toBeGreaterThan(byKey.get("Bavariance/1#10")!.at + DAY);
    expect(byKey.get("Bavariance/1#11")).toMatchObject({ source: "activity", derived: true });
    expect(byKey.get("Bavariance/11#21")).toMatchObject({ source: "activity", derived: true, state: "CLOSED" });
  });

  test("negative control: no planned date in the week and no activity in the week means the card is absent", () => {
    expect(byKey.has("Wladefant/5#3")).toBe(false);
    expect(byKey.has("Bavariance/11#22")).toBe(false);
    expect(placeCards(raw(), BOARDS, WEEK_END + 30 * DAY, WEEK_END + 37 * DAY)).toEqual([]);
  });

  test("a board with more items than its 100-item window is reported as truncated", () => {
    expect(parseCards(RESPONSES.Bavariance, [BOARDS[1]!, BOARDS[2]!]).truncatedBoards).toEqual(["Bavariance/11"]);
  });
});

describe("board and kind filters", () => {
  const block = (sessionId: string, project: string): WeekBlock => ({
    sessionId, project, cwd: `C:/x/${project}`, tool: "veyyon", model: null, firstMessage: null, startMs: T0, endMs: T0 + HOUR,
    commits: 1, commitShas: [sessionId], noCommit: false, boards: [], live: false,
  });
  const full = summarizeWeek({
    weekStart: WEEK_START, zone: "UTC", asOf: T0, boards: BOARDS,
    blocks: [block("a", "super-board"), block("b", "polysimulator"), block("c", "no-board-repo")],
    pullRequests: [
      { repo: "Wladefant/super-board", number: 1, title: "x", url: "u", state: "MERGED", mergedAt: iso(MID), branch: "b" },
      { repo: "Bavariance/polysimulator", number: 2, title: "y", url: "u", state: "OPEN", mergedAt: null, branch: "b" },
      { repo: "Other/repo", number: 3, title: "z", url: "u", state: "OPEN", mergedAt: null, branch: "b" },
    ],
    cards: placeCards(raw(), BOARDS, WEEK_START, WEEK_END),
  });
  const view = (board: string | null, kind: string | null = null) => filterWeek(full, { board, kind }, "UTC");

  test("all returns the unfiltered week", () => {
    expect(view("all")).toBe(full);
    expect(view(null)).toBe(full);
  });

  test("each single board returns only its cards, lanes and pull requests", () => {
    const poly = view("Bavariance/1");
    expect(poly.cards.map(c => c.number)).toEqual([10, 11]);
    expect(poly.blocks.map(b => b.sessionId)).toEqual(["b"]);
    expect(poly.pullRequests.map(p => p.number)).toEqual([2]);
    const ship = view("Bavariance/11");
    expect(ship.cards.map(c => c.number)).toEqual([20, 21]);
    expect(ship.blocks).toEqual([]);
  });

  test("kind filters by board kind", () => {
    expect(view("all", "polysimulator").cards.every(c => c.boardId === "Bavariance/1")).toBe(true);
    expect(view(null, "shipnovo").cards).toHaveLength(2);
    expect(view("Wladefant/5", "shipnovo").cards).toEqual([]);
  });

  test("a lane whose project is on no board appears under unassigned, and only there", () => {
    const un = view("unassigned");
    expect(un.blocks.map(b => b.sessionId)).toEqual(["c"]);
    expect(un.pullRequests.map(p => p.repo)).toEqual(["Other/repo"]);
    expect(un.cards).toEqual([]);
    for (const id of BOARDS.map(b => b.id)) expect(view(id).blocks.some(b => b.sessionId === "c")).toBe(false);
  });

  test("card totals of all equal the sum over the single boards", () => {
    const sum = BOARDS.reduce((total, b) => total + view(b.id).cardTotals.cards, 0);
    expect(full.cardTotals.cards).toBe(sum);
    expect(full.cardTotals.planned + full.cardTotals.derived).toBe(full.cardTotals.cards);
  });
});

describe("cards reader", () => {
  function deps(guardCode = 0) {
    const calls = { guard: 0, graphql: [] as string[] };
    let now = T0;
    const d: GithubReaderDeps = {
      now: () => now,
      guard: async () => { calls.guard++; return guardCode; },
      graphql: async query => { calls.graphql.push(query); return query.includes('"Wladefant"') ? RESPONSES.Wladefant : RESPONSES.Bavariance; },
    };
    return { d, calls, tick: (ms: number) => { now += ms; } };
  }

  test("one query per owner and one guard reading per refresh, then served from the cache", async () => {
    const { d, calls, tick } = deps();
    const reader = new CardsReader(d, BOARDS);
    const first = await reader.read();
    expect(calls.guard).toBe(1);
    expect(calls.graphql).toHaveLength(2); // Wladefant, Bavariance: three boards, two owners
    expect(first.cards).toHaveLength(raw().length);
    expect(calls.graphql.every(q => q.includes("items(last: 100)"))).toBe(true);
    tick(60_000);
    await Promise.all([reader.read(), reader.read()]);
    expect(calls.guard).toBe(1);
    tick(6 * 60_000);
    await reader.read();
    expect(calls.guard).toBe(2);
  });

  test("guard exit 75 makes no GraphQL call and serves stale", async () => {
    const { d, calls } = deps(75);
    const result = await new CardsReader(d, BOARDS).read();
    expect(calls.graphql).toEqual([]);
    expect(result.stale).toBe(true);
    expect(result.cards).toEqual([]);
  });

  test("the query aliases each board of an owner inside one repositoryOwner", () => {
    const q = buildCardsQuery("Bavariance", [BOARDS[1]!, BOARDS[2]!]);
    expect(q).toContain("b0: projectV2(number: 1)");
    expect(q).toContain("b1: projectV2(number: 11)");
    expect(q.match(/repositoryOwner/g)).toHaveLength(1);
  });
});

describe("pull request reader is keyed by week", () => {
  test("a second week is fetched on its own and a refused read never shows the other week's pull requests", async () => {
    let guard = 0;
    let queries = 0;
    const d: GithubReaderDeps = {
      now: () => T0,
      guard: async () => (guard++ < 2 ? 0 : 75),
      graphql: async () => { queries++; return { data: { o0: { nodes: [{ number: queries, title: "t", url: "u", state: "OPEN", mergedAt: null, headRefName: "b", repository: { nameWithOwner: "Wladefant/super-board" } }] } } }; },
    };
    const reader = new GithubReader(d, ["Wladefant"]);
    const [a, b] = await Promise.all([reader.read(WEEK_START), reader.read(WEEK_START + 7 * DAY)]);
    expect(queries).toBe(2);
    expect(a.pullRequests[0]!.number).toBe(1);
    expect(b.pullRequests[0]!.number).toBe(2);
    const refused = await reader.read(WEEK_START - 7 * DAY);
    expect(refused.stale).toBe(true);
    expect(refused.pullRequests).toEqual([]);
  });
});
