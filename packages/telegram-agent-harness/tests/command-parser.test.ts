import { describe, expect, test } from "bun:test";
import { parseTelegramCommand } from "../extension/command-parser";

describe("parseTelegramCommand", () => {
  const ownBot = "superboarddevbot";
  const slotId = "telegram-superboard";

  test("plain command with no bot mention", () => {
    const parsed = parseTelegramCommand("/sessions", ownBot, slotId);
    expect(parsed).not.toBeNull();
    expect(parsed!.command).toBe("sessions");
    expect(parsed!.argument).toBe("");
    expect(parsed!.rawCommand).toBe("/sessions");
    expect(parsed!.botMention).toBeUndefined();
    expect(parsed!.isAddressedToUs).toBe(true);
  });

  test("attached @suffix for our bot", () => {
    const parsed = parseTelegramCommand("/sessions@superboarddevbot", ownBot, slotId);
    expect(parsed).not.toBeNull();
    expect(parsed!.command).toBe("sessions");
    expect(parsed!.argument).toBe("");
    expect(parsed!.rawCommand).toBe("/sessions");
    expect(parsed!.botMention).toBe("superboarddevbot");
    expect(parsed!.isAddressedToUs).toBe(true);
  });

  test("attached @suffix for another bot is rejected", () => {
    const parsed = parseTelegramCommand("/sessions@veyyonbot", ownBot, slotId);
    expect(parsed).not.toBeNull();
    expect(parsed!.command).toBe("sessions");
    expect(parsed!.botMention).toBe("veyyonbot");
    expect(parsed!.isAddressedToUs).toBe(false);
  });

  test("spaced @mention for our bot", () => {
    const parsed = parseTelegramCommand("/sessions @superboarddevbot", ownBot, slotId);
    expect(parsed).not.toBeNull();
    expect(parsed!.command).toBe("sessions");
    expect(parsed!.argument).toBe("");
    expect(parsed!.rawCommand).toBe("/sessions");
    expect(parsed!.botMention).toBe("superboarddevbot");
    expect(parsed!.isAddressedToUs).toBe(true);
  });

  test("spaced @mention with argument before", () => {
    const parsed = parseTelegramCommand("/sessions all @superboarddevbot", ownBot, slotId);
    expect(parsed).not.toBeNull();
    expect(parsed!.command).toBe("sessions");
    expect(parsed!.argument).toBe("all");
    expect(parsed!.rawCommand).toBe("/sessions all");
    expect(parsed!.isAddressedToUs).toBe(true);
  });

  test("spaced @mention with argument after", () => {
    const parsed = parseTelegramCommand("/sessions @superboarddevbot all", ownBot, slotId);
    expect(parsed).not.toBeNull();
    expect(parsed!.command).toBe("sessions");
    expect(parsed!.argument).toBe("all");
    expect(parsed!.rawCommand).toBe("/sessions all");
    expect(parsed!.isAddressedToUs).toBe(true);
  });

  test("spaced @mention with attach argument", () => {
    const parsed = parseTelegramCommand("/attach 1 @superboarddevbot", ownBot, slotId);
    expect(parsed).not.toBeNull();
    expect(parsed!.command).toBe("attach");
    expect(parsed!.argument).toBe("1");
    expect(parsed!.rawCommand).toBe("/attach 1");
    expect(parsed!.isAddressedToUs).toBe(true);
  });

  test("user typo tolerance (@superboreddef matches superboarddevbot)", () => {
    const parsed = parseTelegramCommand("/sessions @superboreddef", ownBot, slotId);
    expect(parsed).not.toBeNull();
    expect(parsed!.command).toBe("sessions");
    expect(parsed!.argument).toBe("");
    expect(parsed!.rawCommand).toBe("/sessions");
    expect(parsed!.isAddressedToUs).toBe(true);
  });

  test("spaced @mention for another bot is rejected", () => {
    const parsed = parseTelegramCommand("/sessions @veyyonbot", ownBot, slotId);
    expect(parsed).not.toBeNull();
    expect(parsed!.isAddressedToUs).toBe(false);
  });

  test("non-command text returns null", () => {
    expect(parseTelegramCommand("hello world", ownBot, slotId)).toBeNull();
  });
});
