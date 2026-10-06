import { runChecks } from "./monitor.mjs";
import targets from "./targets.json";

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(runChecks(env, targets));
  },
};
