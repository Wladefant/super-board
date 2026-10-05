// The signed-out Week page shows its notice and no literal "null" or "undefined" (Refs #480, PR #541).
//
// This loads the real `week/week.js` module into a real DOM (happy-dom) built from the real
// `week/index.html`, with a real HTTP stub behind `fetch`. With no Telegram init data the
// session request is refused, so the page renders its signed-out state. `replaceChildren(null)`
// in that path once painted the text "null" next to the notice; this test fails on it.
import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import { openWeekPage, type WeekPage } from "./fixtures/week-dom";

let page: WeekPage;

beforeAll(async () => {
  page = await openWeekPage(request => {
    const path = new URL(request.url).pathname;
    // Signed out: no init data, so no app session and no week.
    if (path === "/api/session" || path === "/api/week") return Response.json({ error: "Missing Telegram init data." }, { status: 401 });
    return Response.json({ error: "Unexpected" }, { status: 500 });
  });
});

afterAll(() => page.close());

describe("signed-out week page", () => {
  test("shows the sign-in notice and no 'null' or 'undefined' text anywhere", () => {
    const window = page.window;
    const notice = window.document.getElementById("notice")!;
    expect(notice.hidden).toBe(false);
    expect(notice.className).toContain("notice-error");
    // Signing in again is the only way out, so there is no retry button.
    expect(notice.querySelector("button")).toBeNull();
    expect(notice.textContent).toBe(notice.querySelector("span")!.textContent);
    expect(window.document.getElementById("agenda")!.textContent).toBe("Sign in to see the week.");

    const text = window.document.body.textContent ?? "";
    expect(text).not.toMatch(/\bnull\b/i);
    expect(text).not.toMatch(/\bundefined\b/i);
  });
});
