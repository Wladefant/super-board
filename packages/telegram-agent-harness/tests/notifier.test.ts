import { expect, test } from "bun:test";
import { resolve } from "node:path";

test("portable notifier HTML and callback contracts", () => {
  const root = resolve(import.meta.dir, "../../..");
  const result = Bun.spawnSync(["python", "-m", "unittest", "discover", "-s", "workflows/portable", "-p", "test_telegram_notifier.py"], { cwd: root });
  if (result.exitCode !== 0) throw new Error(result.stderr.toString());
  expect(result.exitCode).toBe(0);
}, 60_000);

test.skipIf(!process.env.TG_NOTIFIER_INSTALLED_PATH)("installed notifier HTML and callback contracts", () => {
  const root = resolve(import.meta.dir, "../../..");
  const result = Bun.spawnSync(["python", "-m", "unittest",
    "workflows.portable.test_telegram_notifier.TestMessageFormatting",
    "workflows.portable.test_telegram_notifier.TestDecisionInteractiveCallback",
    "workflows.portable.test_telegram_notifier.TestTelegramNotificationAdapter.test_screenshot_is_native_photo_not_description"], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: process.env.TG_NOTIFIER_INSTALLED_PATH },
  });
  if (result.exitCode !== 0) throw new Error(result.stderr.toString());
  expect(result.exitCode).toBe(0);
}, 60_000);
