// The calendar switcher is one listbox control at every width (#478): a button that opens the
// detail dialog (a popover on desktop, a full-width bottom sheet on a phone) with one option per
// calendar. A native <select> cannot do that on a phone: the OS draws its own picker.
//
// Real `week/index.html` and `week/week.js` in happy-dom, with the typed fixture week behind a stub server.
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { makeWeek } from "./fixtures/week-fixture";
import { openWeekPage, type WeekPage } from "./fixtures/week-dom";

let page: WeekPage;
let weekRequests = 0;

beforeAll(async () => {
  page = await openWeekPage(request => {
    const path = new URL(request.url).pathname;
    if (path === "/api/session") return Response.json({ appSession: "test" });
    if (path === "/api/week") {
      weekRequests += 1;
      return Response.json(makeWeek("normal"));
    }
    return Response.json({ error: "Unexpected" }, { status: 500 });
  }, "/?week=2026-09-28&board=all");
});

afterAll(() => page.close());

const doc = () => page.window.document;
const control = () => doc().getElementById("board")!;
const isOpen = () => doc().getElementById("detail")!.hasAttribute("open");
const options = () => [...doc().querySelectorAll("#detail [role=listbox] [role=option]")];
const boardParam = () => new URL(page.window.location.href).searchParams.get("board");
const key = (target: EventTarget, name: string) =>
  target.dispatchEvent(new page.window.KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true }));

describe("calendar switcher", () => {
  test("is a button that names the current calendar and announces a listbox", () => {
    expect(control().tagName).toBe("BUTTON");
    expect(control().getAttribute("aria-haspopup")).toBe("listbox");
    expect(control().getAttribute("aria-expanded")).toBe("false");
    expect(control().textContent).toContain("All boards");
    expect(isOpen()).toBe(false);
  });

  test("opens a listbox with every calendar and focuses the selected one", () => {
    control().click();
    expect(isOpen()).toBe(true);
    expect(control().getAttribute("aria-expanded")).toBe("true");
    expect(doc().getElementById("detail-title")!.textContent).toBe("Calendar");
    const values = options().map(o => o.getAttribute("data-value"));
    expect(values[0]).toBe("all");
    expect(values).toContain("unassigned");
    expect(values.some(v => v?.startsWith("kind:"))).toBe(true);
    expect(values.length).toBeGreaterThan(4);
    const selected = options().filter(o => o.getAttribute("aria-selected") === "true");
    expect(selected.map(o => o.getAttribute("data-value"))).toEqual(["all"]);
    expect(doc().activeElement).toBe(selected[0]);
    // The page behind the modal dialog is inert, so the name must come from inside the dialog.
    const list = doc().querySelector("#detail [role=listbox]")!;
    const nameIds = list.getAttribute("aria-labelledby")!.split(" ");
    expect(nameIds.map(id => doc().getElementById(id)?.closest("#detail")).every(Boolean)).toBe(true);
    expect(nameIds.map(id => doc().getElementById(id)!.textContent).join(" ")).toBe("Calendar");
  });

  test("arrow keys, Home and End move through the options", () => {
    const list = options();
    key(doc().activeElement!, "ArrowDown");
    expect(doc().activeElement).toBe(list[1]);
    key(doc().activeElement!, "ArrowUp");
    expect(doc().activeElement).toBe(list[0]);
    key(doc().activeElement!, "ArrowUp");
    expect(doc().activeElement).toBe(list[0]);
    key(doc().activeElement!, "End");
    expect(doc().activeElement).toBe(list[list.length - 1]);
    key(doc().activeElement!, "Home");
    expect(doc().activeElement).toBe(list[0]);
  });

  test("Escape closes without a change and returns focus to the switcher", async () => {
    key(doc().activeElement!, "ArrowDown");
    key(doc().activeElement!, "Escape");
    await page.quiescent();
    expect(isOpen()).toBe(false);
    expect(doc().activeElement).toBe(control());
    expect(boardParam()).toBe("all");
  });

  test("Enter picks the focused calendar: URL, storage and label follow, the week stays, no fetch", async () => {
    const before = weekRequests;
    key(control(), "ArrowDown");
    expect(isOpen()).toBe(true);
    key(doc().activeElement!, "ArrowDown");
    const picked = doc().activeElement!;
    const value = picked.getAttribute("data-value")!;
    const label = picked.querySelector(".option-label")!.textContent!;
    const lanes = picked.querySelector(".option-count")!.textContent!;
    const rowsBefore = doc().querySelectorAll("#agenda .row").length;
    expect(value).not.toBe("all");
    key(picked, "Enter");
    await page.quiescent();

    expect(isOpen()).toBe(false);
    expect(doc().activeElement).toBe(control());
    expect(control().getAttribute("aria-expanded")).toBe("false");
    expect(boardParam()).toBe(value);
    expect(new URL(page.window.location.href).searchParams.get("week")).toBe("2026-09-28");
    expect(page.window.localStorage.getItem("superboard.week.board")).toBe(value);
    expect(control().textContent).toContain(label);
    // The loaded week is filtered to the picked calendar: the stats and the agenda follow.
    expect(doc().getElementById("stats")!.textContent).toStartWith(lanes);
    expect(doc().querySelectorAll("#agenda .row").length).toBeLessThan(rowsBefore);
    // The filter runs on the loaded week: picking a calendar fetches nothing and reloads nothing.
    expect(weekRequests).toBe(before);
  });

  test("tapping an option picks it too", () => {
    control().click();
    const all = options().find(o => o.getAttribute("data-value") === "all")!;
    all.dispatchEvent(new page.window.MouseEvent("click", { bubbles: true, cancelable: true }));
    expect(isOpen()).toBe(false);
    expect(boardParam()).toBe("all");
    expect(control().textContent).toContain("All boards");
  });
});

describe("calendar switcher with no week loaded", () => {
  let failed: WeekPage;
  beforeAll(async () => {
    failed = await openWeekPage(request => {
      const path = new URL(request.url).pathname;
      if (path === "/api/session") return Response.json({ appSession: "test" });
      return Response.json({ error: "relay not connected" }, { status: 503 });
    }, "/?week=2026-09-28&board=kind%3Ashipnovo");
  });
  afterAll(() => failed.close());

  test("a board that is not in the list still leaves one option to focus and pick", async () => {
    const d = failed.window.document;
    d.getElementById("board")!.click();
    const opts = [...d.querySelectorAll("#detail [role=listbox] [role=option]")];
    expect(opts.map(o => o.getAttribute("data-value"))).toEqual(["all", "unassigned"]);
    expect(opts.filter(o => o.getAttribute("aria-selected") === "true")).toEqual([]);
    expect(opts.map(o => o.getAttribute("tabindex"))).toEqual(["0", "-1"]);
    expect(d.activeElement).toBe(opts[0]);
    d.activeElement!.dispatchEvent(new failed.window.KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true, cancelable: true }));
    expect(d.activeElement).toBe(opts[1]);
    d.activeElement!.dispatchEvent(new failed.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await failed.quiescent();
    expect(d.getElementById("detail")!.hasAttribute("open")).toBe(false);
    expect(new URL(failed.window.location.href).searchParams.get("board")).toBe("unassigned");
    expect(d.getElementById("board")!.textContent).toContain("Unassigned");
  });
});
